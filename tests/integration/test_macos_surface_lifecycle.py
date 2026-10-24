"""Native endpoint lifetime across consumer closure and abrupt producer death."""
from __future__ import annotations

import ctypes
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

import pytest

pytestmark = pytest.mark.integration

if sys.platform != "darwin":
    pytest.skip("requires macOS IOSurface/Cocoa", allow_module_level=True)

pytest.importorskip("AppKit", reason="PyObjC creates the native consumer window")


@pytest.mark.parametrize("scenario", ["consumer-close", "producer-restart"])
def test_endpoint_lifetime_in_separate_processes(scenario):
    from pylibobs import macos_surfaces

    package_root = Path(macos_surfaces.__file__).resolve().parent.parent
    with subprocess.Popen(
        [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()), scenario],
        env={**os.environ, "PYTHONPATH": str(package_root)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail("Native lifecycle scenario timed out:\n" + (stdout + stderr)[-16000:])
        assert process.returncode == 0, (stdout + stderr)[-16000:]
    assert "SURFACE_LIFECYCLE_COMPLETE" in stdout


def _produce(endpoint, color, connection):
    from pylibobs import OBSContext, Source
    from pylibobs._ffi import ffi, get_lib
    from pylibobs._lib import get_bundled_modules
    from pylibobs.macos_surfaces import SurfaceProducer

    try:
        with OBSContext() as context:
            context.set_video(320, 180, fps_num=30)
            lib = get_lib()
            _, binary, data = next(item for item in get_bundled_modules() if item[0] == "image-source")
            module = ffi.new("obs_module_t **")
            assert lib.obs_open_module(module, binary.encode(), data.encode()) == 0
            assert lib.obs_init_module(module[0])
            lib.obs_post_load_modules()
            source = Source.create("color_source_v3", "lifecycle-color", {
                "width": 320, "height": 180, "color": color,
            })
            lib.obs_source_inc_showing(source._ptr)
            disconnected = False
            try:
                with SurfaceProducer(endpoint, 320, 180) as producer:
                    connection.send({"ready": True})
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        if connection.poll():
                            assert connection.recv() == "stop"
                            break
                        try:
                            producer.render(source, canvas_width=320, canvas_height=180)
                        except ConnectionError:
                            disconnected = True
                            break
                        connection.poll(1 / 60)
                    else:
                        raise AssertionError("Producer did not receive stop or detect disconnection")
                    published = producer.frames_published
            finally:
                lib.obs_source_dec_showing(source._ptr)
        connection.send({"closed": True, "disconnected": disconnected, "published": published})
    except BaseException:
        connection.send({"error": traceback.format_exc()})
        raise
    finally:
        connection.close()


def _center_pixel(window):
    """Validate only the displayed pixel; transport itself performs no readback."""
    import AppKit

    view = window.contentView().subviews()[-1]
    previous = AppKit.NSOpenGLContext.currentContext()
    view.openGLContext().makeCurrentContext()
    gl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
    gl.glReadBuffer.argtypes = [ctypes.c_uint]
    gl.glReadPixels.argtypes = [ctypes.c_int] * 4 + [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    try:
        backing = view.convertRectToBacking_(view.bounds())
        pixel = (ctypes.c_ubyte * 4)()
        gl.glReadBuffer(0x0404)  # GL_FRONT, after the presenter's swap.
        gl.glReadPixels(int(backing.size.width) // 2, int(backing.size.height) // 2,
                        1, 1, 0x1908, 0x1401, pixel)
        return tuple(pixel)
    finally:
        gl.glReadBuffer(0x0405)
        AppKit.NSOpenGLContext.clearCurrentContext()
        if previous is not None:
            previous.makeCurrentContext()


def _scenario(scenario):
    import AppKit
    import objc
    from pylibobs import _ffi
    from pylibobs.macos_surfaces import SurfacePresenter

    AppKit.NSApplication.sharedApplication()
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(50, 50, 320, 180), AppKit.NSWindowStyleMaskTitled,
        AppKit.NSBackingStoreBuffered, False,
    )
    window.setReleasedWhenClosed_(False)
    window.makeKeyAndOrderFront_(None)
    context = multiprocessing.get_context("spawn")
    children = []
    presenter = None
    try:
        presenter = SurfacePresenter(objc.pyobjc_id(window.contentView()))

        def settle(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                presenter.poll()
                if predicate():
                    return
                AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                    AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.01))
            raise AssertionError("Native surface lifecycle state did not settle")

        def start(color):
            parent, child = context.Pipe()
            process = context.Process(target=_produce, args=(presenter.endpoint, color, child))
            process.start()
            child.close()
            children.append((process, parent))
            settle(parent.poll)
            report = parent.recv()
            assert report.get("ready"), report
            return process, parent

        process, connection = start(0xFF0000FF)
        settle(lambda: presenter.frames_presented > 0 and _center_pixel(window) == (255, 0, 0, 255))
        if scenario == "consumer-close":
            presenter.close()
            presenter.close()
            window.close()
            assert not presenter.poll()
            assert connection.poll(10), "Producer did not detect the closed endpoint"
            report = connection.recv()
            assert report.get("closed") and report.get("disconnected"), report
            assert report["published"] > 0
            process.join(5)
            assert process.exitcode == 0
        else:
            assert scenario == "producer-restart"
            process.kill()
            process.join(5)
            assert process.exitcode == -signal.SIGKILL
            replacement, connection = start(0xFFFF0000)
            # A different pixel proves a fresh producer's surfaces were used,
            # rather than counting frames still queued by the killed process.
            settle(lambda: _center_pixel(window) == (0, 0, 255, 255))
            connection.send("stop")
            settle(connection.poll)
            report = connection.recv()
            assert report.get("closed") and not report.get("disconnected"), report
            assert report["published"] > 0
            replacement.join(5)
            assert replacement.exitcode == 0
        assert _ffi._lib is None, "Consumer unexpectedly initialized another libobs engine"
        print("SURFACE_LIFECYCLE_COMPLETE", scenario)
    finally:
        for process, connection in children:
            if process.is_alive():
                process.kill()
            process.join(5)
            connection.close()
            process.close()
        if presenter is not None:
            presenter.close()
        window.close()


if __name__ == "__main__":
    _scenario(sys.argv[1])
