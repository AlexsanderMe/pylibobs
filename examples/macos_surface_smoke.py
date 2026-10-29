"""Two-process Cocoa/IOSurface smoke benchmark (requires PyObjC for the UI).

Run from an installed macOS wheel with bundled OBS runtime::

    python examples/macos_surface_smoke.py --seconds 6
    python examples/macos_surface_smoke.py --seconds 3 --case translucent
    python examples/macos_surface_smoke.py --seconds 3 --case scissored

The parent owns only Cocoa presentation. A spawned process renders a nested
libobs scene; no native view pointers or frame bytes cross the control pipe.
The UI is deliberately stalled once to exercise bounded backpressure, then
resized. JSON reports frame counts, render/poll call durations, and latency from
render entry to completion of GPU presentation (excluding display scanout). Four
consumer-side validation pixels check source colors and opaque letterboxing;
production presentation itself does not read frames back to the CPU.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import platform
import statistics
import time
import traceback


def _timings(samples):
    if len(samples) < 2:
        return {"p50_ms": samples[0] if samples else 0, "p95_ms": samples[0] if samples else 0}
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {"p50_ms": cuts[49], "p95_ms": cuts[94]}


def _sample_presented_pixels(window):
    """Read only four validation pixels from the consumer's displayed buffer."""
    import ctypes
    import AppKit

    gl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
    gl.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    gl.glReadBuffer.argtypes = [ctypes.c_uint]
    view = window.contentView().subviews()[-1]
    context = view.openGLContext()
    previous = AppKit.NSOpenGLContext.currentContext()
    context.makeCurrentContext()
    try:
        backing = view.convertRectToBacking_(view.bounds())
        width, height = int(backing.size.width), int(backing.size.height)
        gl.glReadBuffer(0x0404)  # GL_FRONT, after the presenter's swap
        result = {}
        for name, x, y in (("background", width // 10, height // 2),
                            ("nested_overlay", width // 2, 2 * height // 3),
                            ("lower_background", width // 2, height // 3),
                            ("letterbox", 0, 0)):
            pixel = (ctypes.c_ubyte * 4)()
            gl.glReadPixels(x, y, 1, 1, 0x1908, 0x1401, pixel)  # RGBA / unsigned byte
            result[name] = list(pixel)
        return result
    finally:
        gl.glReadBuffer(0x0405)  # GL_BACK, the presenter's draw buffer
        AppKit.NSOpenGLContext.clearCurrentContext()
        if previous is not None:
            previous.makeCurrentContext()


def _produce(endpoint, seconds, stop, connection, case):
    import resource
    from pylibobs import OBSContext, Scene, Source
    from pylibobs._ffi import ffi, get_lib
    from pylibobs._lib import get_bundled_modules
    from pylibobs.macos_surfaces import SurfaceProducer

    try:
        samples = []
        with OBSContext() as obs:
            obs.set_video(640, 360, fps_num=30)
            lib = get_lib()
            # Load only the plugin needed by this example; no frontend modules.
            module = next((item for item in get_bundled_modules() if item[0] == "image-source"), None)
            if module is None:
                raise RuntimeError("This example requires the bundled image-source plugin")
            _, binary, data = module
            pointer = ffi.new("obs_module_t **")
            if lib.obs_open_module(pointer, binary.encode(), data.encode()) != 0 or not lib.obs_init_module(pointer[0]):
                raise RuntimeError("Cannot load image-source plugin")
            lib.obs_post_load_modules()
            scene = Scene.create("surface-example")
            nested = Scene.create("nested-example")
            background = Source.create("color_source_v3", "background", {
                "width": 640, "height": 360, "color": 0xFF8A4520,
            })
            overlay = Source.create("color_source_v3", "overlay", {
                "width": 200, "height": 120, "color": 0xFF208AE8,
            })
            scene.add(background)
            nested.add(overlay)
            item = scene.add(nested.as_source())
            item.pos = (220, 60)
            source = scene.as_source()
            if case == "translucent":
                source = Source.create("color_source_v3", "translucent", {
                    "width": 640, "height": 360, "color": 0x800000FF,
                })
            lib.obs_source_inc_showing(source._ptr)
            started = time.monotonic()
            try:
                with SurfaceProducer(endpoint, 640, 360) as producer:
                    if case == "scissored":
                        import ctypes
                        gl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
                        gl.glEnable.argtypes = gl.glDisable.argtypes = [ctypes.c_uint]
                        gl.glScissor.argtypes = [ctypes.c_int] * 4
                        gl.glIsEnabled.argtypes = [ctypes.c_uint]
                        gl.glIsEnabled.restype = ctypes.c_ubyte
                        original_publish = producer._library.pys_producer_publish
                        def scissored_publish(*arguments):
                            # A caller's scissor must not clip the transport's full-surface copy.
                            gl.glEnable(0x0C11)  # GL_SCISSOR_TEST
                            gl.glScissor(0, 0, 1, 1)
                            try:
                                result = original_publish(*arguments)
                                assert gl.glIsEnabled(0x0C11), "Native copy changed caller's scissor enable state"
                                return result
                            finally:
                                gl.glDisable(0x0C11)
                        producer._library.pys_producer_publish = scissored_publish
                    while not stop.is_set() and time.monotonic() - started < seconds:
                        tick = time.perf_counter()
                        producer.render(source, canvas_width=640, canvas_height=360)
                        samples.append((time.perf_counter() - tick) * 1000)
                        stop.wait(max(0, 1 / 30 - (time.perf_counter() - tick)))
                    metrics = {
                        "published": producer.frames_published,
                        "dropped": producer.frames_dropped,
                        "render": _timings(samples),
                        "elapsed_seconds": time.monotonic() - started,
                    }
            finally:
                lib.obs_source_dec_showing(source._ptr)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        metrics.update(cpu_seconds=usage.ru_utime + usage.ru_stime, peak_rss_bytes=usage.ru_maxrss)
        connection.send(metrics)
    except BaseException:
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--case", choices=("graph", "translucent", "scissored"), default="graph")
    args = parser.parse_args()
    if platform.system() != "Darwin" or args.seconds < 2:
        parser.error("requires macOS and --seconds >= 2")
    import AppKit
    import objc
    from pylibobs import _ffi
    from pylibobs.macos_surfaces import SurfacePresenter

    AppKit.NSApplication.sharedApplication()
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(120, 120, 640, 360),
        AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskResizable,
        AppKit.NSBackingStoreBuffered, False,
    )
    window.setReleasedWhenClosed_(False)
    window.setTitle_("pylibobs IOSurface smoke test")
    window.makeKeyAndOrderFront_(None)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    stop = context.Event()
    child = None
    samples = []
    latencies = []
    resized = stalled = False
    pixels = None
    try:
        with SurfacePresenter(objc.pyobjc_id(window.contentView())) as presenter:
            assert _ffi._lib is None, "UI process unexpectedly loaded libobs"
            child = context.Process(target=_produce, args=(presenter.endpoint, args.seconds, stop, send, args.case))
            child.start()
            send.close()
            deadline = time.monotonic() + args.seconds + 20
            metrics = None
            while time.monotonic() < deadline:
                if metrics is None and receive.poll():
                    metrics = receive.recv()
                tick = time.perf_counter()
                presented = presenter.poll()
                if presented:
                    latencies.append(presenter.last_frame_latency_ms)
                samples.append((time.perf_counter() - tick) * 1000)
                if presenter.frames_presented and not stalled:
                    time.sleep(0.4)
                    stalled = True
                if presenter.frames_presented >= 3 and not resized:
                    window.setContentSize_(AppKit.NSMakeSize(800, 500))
                    resized = True
                elif resized and presented and pixels is None:
                    pixels = _sample_presented_pixels(window)
                AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                    AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.01)
                )
                # Producer completion does not mean the last delivered frame
                # has already been presented in the resized window.
                if metrics is not None and not presented:
                    break
            if metrics is None:
                raise RuntimeError("Producer timed out")
            if "error" in metrics:
                raise RuntimeError(metrics["error"])
            metrics.update(presented=presenter.frames_presented, resized=resized,
                           ui_loaded_libobs=_ffi._lib is not None, poll=_timings(samples),
                           render_to_present=_timings(latencies), pixels=pixels)
            print(json.dumps(metrics, indent=2))
            assert metrics["published"] > 0 and metrics["presented"] > 0
            assert metrics["dropped"] > 0, "Intentional UI stall did not exercise backpressure"
            assert resized and not metrics["ui_loaded_libobs"]
            assert pixels is not None, "No presented pixels were sampled"
            expected = {"background": (32, 69, 138, 255),
                        "nested_overlay": (232, 138, 32, 255),
                        "lower_background": (32, 69, 138, 255), "letterbox": (0, 0, 0, 255)}
            if args.case == "translucent":
                expected.update(background=(188, 0, 0, 255), nested_overlay=(188, 0, 0, 255),
                                lower_background=(188, 0, 0, 255))
            for name, reference in expected.items():
                matches = all(abs(actual - value) <= 3
                              for actual, value in zip(pixels[name], reference))
                assert matches, (name, pixels[name], reference)
    finally:
        stop.set()
        if child is not None:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        receive.close()
        window.close()


if __name__ == "__main__":
    main()
