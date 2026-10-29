"""Real Cocoa display lifecycle; requires a logged-in graphical macOS session."""
from __future__ import annotations

import platform
import time

import pytest

pytestmark = pytest.mark.integration

if platform.system() != "Darwin":
    pytest.skip("requires Cocoa", allow_module_level=True)

AppKit = pytest.importorskip("AppKit", reason="PyObjC is needed only for this GUI test")
objc = pytest.importorskip("objc")


def test_nsview_display_draw_resize_and_release():
    from pylibobs import Display, OBSContext, Scene
    from pylibobs.display import render_source_letterboxed
    from pylibobs._ffi import ffi

    AppKit.NSApplication.sharedApplication()
    frame = AppKit.NSMakeRect(0, 0, 320, 180)
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False,
    )
    window.setReleasedWhenClosed_(False)
    view = AppKit.NSView.alloc().initWithFrame_(frame)
    window.setContentView_(view)
    display = None
    try:
        with OBSContext() as obs:
            obs.set_video(320, 180, fps_num=30)
            scene = Scene.create("cocoa-display")
            source = scene.as_source()
            rect = view.convertRectToBacking_(view.bounds())
            display = Display.from_window(
                objc.pyobjc_id(view), int(rect.size.width), int(rect.size.height),
            )
            observed_sizes = set()

            def draw(width, height):
                render_source_letterboxed(source._ptr, 320, 180, width, height)
                observed_sizes.add((width, height))

            display.add_draw_callback(draw)
            view.setFrameSize_(AppKit.NSMakeSize(640, 360))
            rect = view.convertRectToBacking_(view.bounds())
            expected = (int(rect.size.width), int(rect.size.height))
            display.resize(*expected)
            deadline = time.monotonic() + 5
            while expected not in observed_sizes and time.monotonic() < deadline:
                AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                    AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.02)
                )
            assert expected in observed_sizes
            assert display.size == expected
            display.enabled = False
            assert not display.enabled
            display.release()
            display.release()
            assert display._ptr == ffi.NULL
    finally:
        if display is not None:
            display.release()
        window.close()
