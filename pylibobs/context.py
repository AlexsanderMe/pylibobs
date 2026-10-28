"""
OBSContext — lifecycle manager for the libobs runtime.

Usage::

    with OBSContext() as obs:
        obs.set_video(width=1920, height=1080, fps_num=60)
        obs.set_audio()
        obs.load_modules()
        ...
"""

from __future__ import annotations

import platform
import warnings
from pathlib import Path
from enum import IntEnum

from ._ffi import ffi, get_lib, mark_alive, mark_dead, release_all_wrappers


# libobs MAX_CHANNELS is 64 in the public ABI; conservatively clear that many.
_MAX_OUTPUT_CHANNELS = 64
from ._lib import QT_FRONTEND_MODULES, discover_modules, get_graphics_module, get_obs_data_dir


class VideoFormat(IntEnum):
    NONE  = 0
    I420  = 1
    NV12  = 2
    YVYU  = 3
    YUY2  = 4
    UYVY  = 5
    RGBA  = 6
    BGRA  = 7
    BGRX  = 8
    Y800  = 9
    I444  = 10
    BGR3  = 11
    I422  = 12


class Speakers(IntEnum):
    UNKNOWN  = 0
    MONO     = 1
    STEREO   = 2
    S2POINT1 = 3
    S4POINT0 = 4
    S4POINT1 = 5
    S5POINT1 = 6
    S7POINT1 = 8


class ScaleType(IntEnum):
    DISABLE  = 0
    POINT    = 1
    BICUBIC  = 2
    BILINEAR = 3
    LANCZOS  = 4
    AREA     = 5


class ColorSpace(IntEnum):
    DEFAULT   = 0
    CS601     = 1
    CS709     = 2
    SRGB      = 3
    CS2100_PQ = 4
    CS2100_HLG= 5


class VideoRange(IntEnum):
    DEFAULT = 0
    PARTIAL = 1
    FULL    = 2


_VIDEO_INIT_ERR = {
    ffi.typeof("enum obs_video_err").relements[name]: reason
    for name, reason in {
        "OBS_VIDEO_SUCCESS": "success",
        "OBS_VIDEO_FAIL": "general failure",
        "OBS_VIDEO_NOT_SUPPORTED": "not supported (check graphics driver/module)",
        "OBS_VIDEO_INVALID_PARAM": "invalid parameters",
        "OBS_VIDEO_CURRENTLY_ACTIVE": "video already active",
        "OBS_VIDEO_MODULE_NOT_FOUND": "graphics module not found",
    }.items()
}



class NixPlatform(IntEnum):
    """libobs Linux/*BSD windowing platform (``obs_nix_platform_type``)."""

    INVALID = 0
    X11_EGL = 1
    WAYLAND = 2


def set_nix_platform(platform: int) -> None:
    """Select the Linux/*BSD windowing platform. Call BEFORE ``set_video``.

    Only meaningful on Linux/*BSD builds of libobs.
    """
    get_lib().obs_set_nix_platform(int(platform))


def get_nix_platform() -> int:
    return int(get_lib().obs_get_nix_platform())


def set_nix_platform_display(display: int) -> None:
    """Share the host toolkit's X/Wayland display with libobs.

    ``display`` is an integer pointer (e.g. Qt's Xlib ``Display*``). Setting it
    before ``set_video`` makes libobs' EGL reuse the toolkit's connection rather
    than opening a second one — the fix for ``EGL_BAD_ACCESS`` when embedding a
    libobs display next to a Qt/GTK OpenGL context on the same GPU.
    """
    get_lib().obs_set_nix_platform_display(ffi.cast("void *", int(display)))


def get_nix_platform_display() -> int:
    return int(ffi.cast("uintptr_t", get_lib().obs_get_nix_platform_display()))


#: libobs major version pylibobs's declarations are written for.
LIBOBS_MAJOR = 33


class OBSContext:
    """
    Context manager that owns the libobs runtime lifecycle.

    Parameters
    ----------
    locale:             BCP-47 locale string (e.g. "en-US").
    module_config_path: Directory where OBS modules store their config.
                        Defaults to the OS-standard OBS config dir.
    """

    def __init__(
        self,
        locale: str = "en-US",
        module_config_path: str | None = None,
    ) -> None:
        self._locale = locale
        self._module_config_path = module_config_path or _default_config_dir()
        self._started = False
        #: ``(name, reason)`` for each module the last load_modules() could not load.
        self.failed_modules: list[tuple[str, str]] = []
        # obs_video_info retains the supplied graphics_module pointer.
        self._graphics_module_buffers: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self) -> None:
        if self._started:
            return
        lib = get_lib()
        if lib.obs_initialized():
            # Some other code already initialised libobs; don't fight it.
            self._started = True
            return
        locale_b = self._locale.encode()
        config_b = self._module_config_path.encode() if self._module_config_path else ffi.NULL
        ok = lib.obs_startup(locale_b, config_b, ffi.NULL)
        if not ok:
            raise RuntimeError("obs_startup() returned false — libobs failed to initialize.")
        self._started = True
        mark_alive()

        # pylibobs's declarations describe one libobs major version. A system
        # libobs of another major (pylibobs fell back to it because nothing is
        # bundled) may lack functions or lay structs out differently.
        major = int(lib.obs_get_version()) >> 24
        if major != LIBOBS_MAJOR:
            warnings.warn(f"pylibobs targets libobs {LIBOBS_MAJOR}.x but loaded "
                          f"{ffi.string(lib.obs_get_version_string()).decode()}; "
                          "some calls may fail or misbehave", RuntimeWarning, stacklevel=2)
        else:
            # OBS 33 added monitor on/off hotkeys to every audio source. libobs
            # only names the four older ones (and only the OBS frontend sets the
            # rest), so without this their descriptions are NULL.
            lib.obs_hotkeys_set_audio_hotkeys_translations(
                b"Mute", b"Unmute", b"Push-to-mute", b"Push-to-talk",
                b"Enable Monitoring", b"Disable Monitoring")

        # Register the bundled data path so shader effects etc. resolve.
        data_dir = get_obs_data_dir()
        if data_dir:
            lib.obs_add_data_path((data_dir.rstrip("/\\") + "/").encode())
            # libobs subdir for shaders
            libobs_sub = data_dir.rstrip("/\\") + "/libobs/"
            lib.obs_add_data_path(libobs_sub.encode())

    def shutdown(self) -> None:
        """
        Shut down libobs cleanly.

        Order matters here — obs_shutdown() does not gracefully handle
        outstanding refcounts on sources/outputs/etc.:
          1. Stop and clear all output channels (drops libobs's internal refs)
          2. Release every Python wrapper we created (drops our refs)
          3. Run a gc pass so any remaining wrappers get finalized
          4. Call obs_shutdown() — now all refs are gone
        """
        if not self._started:
            return

        lib = get_lib()
        try:
            # Step 1: clear all output channels
            for ch in range(_MAX_OUTPUT_CHANNELS):
                try:
                    lib.obs_set_output_source(ch, ffi.NULL)
                except Exception:
                    pass

            # Step 2 + 3: release wrappers, then gc anything else
            release_all_wrappers()
            import gc
            gc.collect()

            # Many sources (color_source, text, image, …) defer destruction of
            # their GPU resources to libobs's video/graphics thread. Releasing
            # the last reference only *queues* that destroy; if obs_shutdown
            # then tears the graphics thread down while a destroy is still in
            # flight, the callback runs on a dead graphics context and
            # segfaults (obs_source_release → … on a worker thread).
            #
            # obs_wait_for_destroy_queue() is libobs's own barrier for exactly
            # this — it round-trips the video thread until the deferred-destroy
            # queue is empty, which is what the OBS frontend does on shutdown.
            # Bounded loop: each call drains one batch; stop when it reports
            # nothing left (or after a sane cap, so a stuck thread can't hang
            # shutdown forever).
            try:
                for _ in range(10):
                    if not lib.obs_wait_for_destroy_queue():
                        break
            except Exception:
                pass

            # Step 4: tear down libobs
            lib.obs_shutdown()
        finally:
            mark_dead()
            self._started = False
            self._graphics_module_buffers.clear()

    # ------------------------------------------------------------------
    # Video / audio reset
    # ------------------------------------------------------------------

    def set_video(
        self,
        width: int = 1920,
        height: int = 1080,
        fps_num: int = 30,
        fps_den: int = 1,
        output_width: int | None = None,
        output_height: int | None = None,
        output_format: VideoFormat = VideoFormat.NV12,
        colorspace: ColorSpace = ColorSpace.DEFAULT,
        range: VideoRange = VideoRange.DEFAULT,
        scale_type: ScaleType = ScaleType.BICUBIC,
        adapter: int = 0,
        graphics_module: str | None = None,
    ) -> None:
        """Call obs_reset_video with the given parameters."""
        lib = get_lib()
        ovi = ffi.new("struct obs_video_info *")

        gfx = graphics_module or get_graphics_module()
        if gfx not in self._graphics_module_buffers:
            self._graphics_module_buffers[gfx] = ffi.new("char[]", gfx.encode())
        ovi.graphics_module = self._graphics_module_buffers[gfx]
        ovi.fps_num        = fps_num
        ovi.fps_den        = fps_den
        ovi.base_width     = width
        ovi.base_height    = height
        ovi.output_width   = output_width or width
        ovi.output_height  = output_height or height
        ovi.output_format  = int(output_format)
        ovi.gpu_conversion = True
        ovi.colorspace     = int(colorspace)
        ovi.range          = int(range)
        ovi.scale_type     = int(scale_type)
        ovi.adapter        = adapter

        err = lib.obs_reset_video(ovi)
        if err != 0:
            msg = _VIDEO_INIT_ERR.get(err, f"unknown error {err}")
            raise RuntimeError(f"obs_reset_video failed: {msg}")

    def set_audio(
        self,
        samples_per_sec: int = 44100,
        speakers: Speakers = Speakers.STEREO,
    ) -> None:
        """Call obs_reset_audio with the given parameters."""
        lib = get_lib()
        oai = ffi.new("struct obs_audio_info *")
        oai.samples_per_sec = samples_per_sec
        oai.speakers        = int(speakers)
        ok = lib.obs_reset_audio(oai)
        if not ok:
            raise RuntimeError("obs_reset_audio failed.")

    # ------------------------------------------------------------------
    # Main-mix handles — raw video_t*/audio_t* for Output.set_media()
    # ------------------------------------------------------------------

    def get_video(self):
        """Return the main mix's ``video_t*`` (raw cffi handle), or None."""
        ptr = get_lib().obs_get_video()
        return ptr if ptr != ffi.NULL else None

    def get_audio(self):
        """Return the main mix's ``audio_t*`` (raw cffi handle), or None."""
        ptr = get_lib().obs_get_audio()
        return ptr if ptr != ffi.NULL else None

    # ------------------------------------------------------------------
    # Module loading
    # ------------------------------------------------------------------

    def add_module_path(self, bin_path: str, data_path: str) -> None:
        """Also load the plugins in ``bin_path`` on the next :meth:`load_modules`.

        ``data_path`` may contain ``%module%``. Since OBS 33 registering a path
        with libobs no longer loads anything by itself (``obs_load_all_modules``
        became a no-op), so load_modules() enumerates these paths with libobs's
        own ``obs_find_modules`` and opens each module.
        """
        get_lib().obs_add_module_path(bin_path.encode(), data_path.encode())

    def load_modules(self, auto_detect: bool = True) -> None:
        """Load OBS plugin modules.

        Modules are always loaded explicitly, one by one: the plugins bundled
        inside the wheel when present, otherwise those of a system OBS install
        (``auto_detect``), plus any added with :meth:`add_module_path`. libobs's
        own scan is not used — it is a no-op since OBS 33, and on older systems
        it would pick up OBS's Qt frontend plugins, which abort the process
        without a QApplication.

        Modules that fail to load are recorded in :attr:`failed_modules`; a
        warning is issued if none could be loaded at all.
        """
        lib = get_lib()
        modules = discover_modules() if auto_detect else []
        if int(lib.obs_get_version()) >> 24 < LIBOBS_MAJOR and not modules:
            # A pre-33 system libobs with nothing found here: its own scan
            # still works and knows its install paths.
            lib.obs_load_all_modules()
        seen = {name for name, _, _ in modules}
        for module in _modules_in_added_paths():
            if module[0] not in seen and module[0] not in QT_FRONTEND_MODULES:
                seen.add(module[0])
                modules.append(module)

        self.failed_modules = []
        loaded = 0
        for name, bin_path, data_path in modules:
            reason = self._open_and_init_module(name, bin_path, data_path)
            if reason is None:
                loaded += 1
            else:
                self.failed_modules.append((name, reason))
        lib.obs_post_load_modules()

        if modules and not loaded:
            warnings.warn(f"no OBS module could be loaded ({len(modules)} found): "
                          f"{self.failed_modules[:3]}", RuntimeWarning, stacklevel=2)
        elif not modules and auto_detect:
            warnings.warn("no OBS plugin modules found — run scripts/fetch_libs.py or "
                          "install OBS Studio; only libobs's built-in types are available",
                          RuntimeWarning, stacklevel=2)

    _MODULE_ERRORS = {
        -1: "generic error", -2: "file not found", -3: "missing obs_module_load export",
        -4: "incompatible libobs version", -5: "hardcoded to be skipped",
    }

    def _open_and_init_module(self, name: str, bin_path: str, data_path: str) -> str | None:
        """Open and initialise one module; ``None`` on success, else the reason."""
        lib = get_lib()
        if lib.obs_get_module(name.encode()) != ffi.NULL:
            return None                                  # already loaded
        module_pp = ffi.new("obs_module_t **")
        rc = lib.obs_open_module(module_pp, bin_path.encode(), data_path.encode())
        if rc != 0:                                      # MODULE_SUCCESS
            return self._MODULE_ERRORS.get(rc, f"error {rc}")
        if not lib.obs_init_module(module_pp[0]):
            return "obs_module_load() returned false"
        return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def initialized(self) -> bool:
        return bool(get_lib().obs_initialized())

    @property
    def version(self) -> str:
        s = get_lib().obs_get_version_string()
        return ffi.string(s).decode() if s != ffi.NULL else ""

    # ------------------------------------------------------------------
    # Video timing / state introspection
    # ------------------------------------------------------------------

    @property
    def video_frame_time_ns(self) -> int:
        """Current frame's timestamp in nanoseconds (from libobs's video clock)."""
        return int(get_lib().obs_get_video_frame_time())

    @property
    def hdr_nominal_peak_level(self) -> float:
        return float(get_lib().obs_get_video_hdr_nominal_peak_level())

    @property
    def sdr_white_level(self) -> float:
        return float(get_lib().obs_get_video_sdr_white_level())

    def set_video_levels(self, sdr_white: float, hdr_nominal_peak: float) -> None:
        get_lib().obs_set_video_levels(float(sdr_white), float(hdr_nominal_peak))

    def get_current_video_info(self):
        """Read the currently-active video settings as a dict, or None if
        video isn't initialised yet."""
        ovi = ffi.new("struct obs_video_info *")
        if not get_lib().obs_get_video_info(ovi):
            return None
        return {
            "fps_num":       int(ovi.fps_num),
            "fps_den":       int(ovi.fps_den),
            "base_width":    int(ovi.base_width),
            "base_height":   int(ovi.base_height),
            "output_width":  int(ovi.output_width),
            "output_height": int(ovi.output_height),
            "output_format": int(ovi.output_format),
            "adapter":       int(ovi.adapter),
            "gpu_conversion": bool(ovi.gpu_conversion),
            "colorspace":    int(ovi.colorspace),
            "range":         int(ovi.range),
            "scale_type":    int(ovi.scale_type),
        }

    # ------------------------------------------------------------------
    # Audio state
    # ------------------------------------------------------------------

    def reset_audio_monitoring(self) -> None:
        """Re-enumerate audio monitoring devices (call after USB headset
        plug/unplug)."""
        get_lib().obs_reset_audio_monitoring()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "OBSContext":
        self.startup()
        return self

    def __exit__(self, *_) -> None:
        self.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _modules_in_added_paths() -> list[tuple[str, str, str]]:
    """``(name, bin_path, data_path)`` of every module under the paths given to
    ``obs_add_module_path``, found by libobs but not loaded."""
    found: list[tuple[str, str, str]] = []

    @ffi.callback("void(void *, const struct obs_module_info *)")
    def _cb(_param, info):
        bin_path = ffi.string(info.bin_path).decode("utf-8", "replace")
        data_path = ffi.string(info.data_path).decode("utf-8", "replace")
        name = Path(bin_path).stem
        found.append((name, bin_path, data_path))

    get_lib().obs_find_modules(_cb, ffi.NULL)
    return found


def _default_config_dir() -> str:
    import os
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "obs-studio", "plugin-config")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/obs-studio/plugin-config")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return os.path.join(xdg, "obs-studio", "plugin-config")
