"""Bounded GPU surface delivery between macOS processes, independent of GUI toolkit.

Create/poll/close the presenter on the Cocoa main thread, before destroying its
borrowed parent NSView. The producer requires an initialized libobs OpenGL
context. Only the producer imports libobs; the receiving process needs Cocoa
and this small native library. Endpoints are private, random bootstrap names;
Mach descriptors transfer rights rather than process-local pointer values.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import platform
import threading
import uuid


def _native():
    if platform.system() != "Darwin":
        raise OSError("IOSurface delivery requires macOS")
    path = Path(__file__).parent / "native" / "macos_surfaces.dylib"
    if not path.is_file():
        # Source installs skip the helper when clang is unavailable.
        raise OSError(
            f"pylibobs's IOSurface helper is missing ({path}). Install a macOS wheel, "
            "or install the Xcode command line tools (xcode-select --install) "
            "and reinstall pylibobs from source."
        )
    library = ctypes.CDLL(str(path))
    signatures = {
        "pys_presenter_create": ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p),
        "pys_presenter_poll": ([ctypes.c_void_p], ctypes.c_int),
        "pys_presenter_close": ([ctypes.c_void_p], None),
        "pys_clock_ns": ([], ctypes.c_uint64),
        "pys_presenter_latency_ns": ([ctypes.c_void_p], ctypes.c_uint64),
        "pys_producer_create": ([ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_void_p),
        "pys_producer_publish": ([ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint64], ctypes.c_int),
        "pys_producer_close": ([ctypes.c_void_p], None),
    }
    for name, (args, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
    return library


def _main_thread():
    if platform.system() != "Darwin":
        raise OSError("Cocoa presentation requires macOS")
    main = ctypes.CDLL(None).pthread_main_np
    main.restype, main.argtypes = ctypes.c_int, []
    if not main():
        raise RuntimeError("Cocoa presentation requires the main thread")


class SurfacePresenter:
    """Present complete frames in a child of a borrowed, same-process NSView.

    ``endpoint`` is serializable connection metadata, not a window pointer.
    Call ``poll`` from the UI event loop. It presents the latest queued frame
    and releases older frames, so a slow UI cannot build an unbounded queue.
    """

    def __init__(self, nsview: int):
        _main_thread()
        if not isinstance(nsview, int) or isinstance(nsview, bool) or nsview <= 0:
            raise ValueError("Expected a live same-process NSView address")
        self._library = _native()
        self.endpoint = "org.pylibobs.surfaces." + uuid.uuid4().hex
        self._pointer = self._library.pys_presenter_create(nsview, self.endpoint.encode())
        if not self._pointer:
            raise RuntimeError("Unable to create the Cocoa surface presenter")
        self.frames_presented = 0

    def poll(self) -> bool:
        _main_thread()
        if not self._pointer:
            return False
        status = self._library.pys_presenter_poll(self._pointer)
        if status < 0:
            raise RuntimeError("Cocoa surface presentation failed")
        self.frames_presented += status
        return bool(status)

    def close(self) -> None:
        _main_thread()
        pointer, self._pointer = self._pointer, None
        if pointer:
            self._library.pys_presenter_close(pointer)

    @property
    def last_frame_latency_ms(self) -> float:
        """Last frame's render-to-GPU-presentation latency, excluding scanout."""
        _main_thread()
        return self._library.pys_presenter_latency_ns(self._pointer) / 1_000_000 if self._pointer else 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class SurfaceProducer:
    """Render any source/transition to a bounded pool of shared GPU surfaces.

    Call ``render`` at the desired cadence, normally from libobs's main render
    callback. A busy consumer drops new frames; no frame bytes cross Python.
    Instances serialize rendering/close and release GL resources before OBS
    shutdown. A dimension change requires replacing this producer.
    """

    def __init__(self, endpoint: str, width: int, height: int):
        if not isinstance(endpoint, str) or not endpoint.startswith("org.pylibobs.surfaces.") or len(endpoint) > 120:
            raise ValueError("Invalid surface endpoint")
        for value in (width, height):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 16384:
                raise ValueError("Surface dimensions must be positive integers <= 16384")
        from ._ffi import ffi, get_lib
        from ._lib import find_libobs

        self._ffi, self._lib = ffi, get_lib()
        self._library = _native()
        self._lock = threading.RLock()
        self.width, self.height = width, height
        self.frames_published = self.frames_dropped = 0
        self._pointer = None
        self._texture = ffi.NULL
        native_obs = ctypes.CDLL(find_libobs())
        self._texture_object = native_obs.gs_texture_get_obj
        self._texture_object.argtypes = [ctypes.c_void_p]
        self._texture_object.restype = ctypes.POINTER(ctypes.c_uint)
        self._lib.obs_enter_graphics()
        try:
            self._pointer = self._library.pys_producer_create(endpoint.encode(), width, height)
            if not self._pointer:
                raise RuntimeError("Unable to connect/create IOSurface producer")
            self._texture = self._lib.gs_texrender_create(5, 0)
            if self._texture == ffi.NULL:
                raise RuntimeError("Unable to allocate surface render target")
            from ._ffi import register_wrapper
            register_wrapper(self)
        except BaseException:
            self.close()
            raise
        finally:
            self._lib.obs_leave_graphics()

    def render(self, source, *, canvas_width: int, canvas_height: int, before_render=None) -> bool:
        """Render a borrowed Source wrapper or native ``obs_source_t *``.

        Keep the native source alive until this call returns, including while
        waiting for the graphics context and running ``before_render``. For a
        borrowed wrapper, keep its owning scene/item alive as well. Coordinate
        release with the render loop; producer locks protect only the producer.
        ``before_render`` must return false if it invalidates the source.
        ``None`` or NULL renders opaque black.
        """
        if canvas_width <= 0 or canvas_height <= 0:
            raise ValueError("Canvas dimensions must be positive")
        ffi, lib = self._ffi, self._lib
        pointer = getattr(source, "_ptr", source)
        if not self._pointer:
            return False
        # Callbacks may already own the graphics mutex. Always acquire it
        # before the Python lifecycle lock, including on concurrent close.
        lib.obs_enter_graphics()
        try:
            with self._lock:
                if not self._pointer:
                    return False
                if before_render is not None and not before_render():
                    return False
                started = self._library.pys_clock_ns()
                lib.gs_texrender_reset(self._texture)
                if not lib.gs_texrender_begin(self._texture, self.width, self.height):
                    return False
                try:
                    color = ffi.new("struct vec4 *", {"x": 0, "y": 0, "z": 0, "w": 1})
                    lib.gs_clear(1, color, 0.0, 0)
                    lib.gs_ortho(0.0, float(canvas_width), 0.0, float(canvas_height), -100.0, 100.0)
                    lib.gs_blend_state_push()
                    try:
                        lib.gs_reset_blend_state()
                        if pointer is not None and pointer != ffi.NULL:
                            lib.obs_source_video_render(pointer)
                    finally:
                        lib.gs_blend_state_pop()
                finally:
                    lib.gs_texrender_end(self._texture)
                texture = lib.gs_texrender_get_texture(self._texture)
                address = int(ffi.cast("uintptr_t", texture))
                native_texture = self._texture_object(address)
                if not native_texture:
                    raise RuntimeError("libobs render texture unavailable")
                status = self._library.pys_producer_publish(self._pointer, native_texture[0], 0x0DE1, started)
                if status < 0:
                    raise ConnectionError("Surface presenter disconnected or render failed")
                self.frames_published += status
                self.frames_dropped += not status
                return bool(status)
        finally:
            lib.obs_leave_graphics()

    def close(self) -> None:
        if not self._pointer and self._texture == self._ffi.NULL:
            return
        self._lib.obs_enter_graphics()
        try:
            with self._lock:
                pointer, self._pointer = self._pointer, None
                if pointer:
                    self._library.pys_producer_close(pointer)
                if self._texture != self._ffi.NULL:
                    self._lib.gs_texrender_destroy(self._texture)
                    self._texture = self._ffi.NULL
        finally:
            self._lib.obs_leave_graphics()

    def release(self) -> None:
        """Release before OBS shutdown, including registry-managed teardown."""
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
