"""Native display contracts without a renderer or a GUI toolkit."""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylibobs import display as module
from pylibobs._ffi import ffi


@pytest.fixture
def backend(monkeypatch):
    init = SimpleNamespace(window=SimpleNamespace())
    fake_ffi = Mock(wraps=ffi)
    fake_ffi.NULL = ffi.NULL
    fake_ffi.new.return_value = init
    lib = Mock()
    lib.obs_display_create.return_value = ffi.cast("obs_display_t *", 0x1230)
    monkeypatch.setattr(module, "ffi", fake_ffi)
    monkeypatch.setattr(module, "get_lib", lambda: lib)
    monkeypatch.setattr(module, "register_wrapper", lambda _: None)
    monkeypatch.setattr(module, "is_alive", lambda: True)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module, "_require_cocoa_main_thread", Mock())
    return lib, init


def test_cocoa_passes_borrowed_view_and_backing_dimensions(backend):
    lib, init = backend
    display = module.Display.from_window(0x4560, 1280, 720)
    assert int(ffi.cast("uintptr_t", init.window.view)) == 0x4560
    assert (init.cx, init.cy) == (1280, 720)
    assert not hasattr(init.window, "hwnd")
    display.release()
    display.release()
    lib.obs_display_destroy.assert_called_once()
    assert display._ptr == ffi.NULL


def test_cocoa_rejects_worker_before_loading_libobs(backend):
    lib, _ = backend
    module._require_cocoa_main_thread.side_effect = RuntimeError("main thread")
    with pytest.raises(RuntimeError, match="main thread"):
        module.Display.from_window(0x4560, 1280, 720)
    lib.obs_display_create.assert_not_called()


def test_cocoa_failed_creation_does_not_register_wrapper(backend, monkeypatch):
    lib, _ = backend
    lib.obs_display_create.return_value = ffi.NULL
    register = Mock()
    monkeypatch.setattr(module, "register_wrapper", register)
    with pytest.raises(RuntimeError, match="obs_display_create returned NULL"):
        module.Display.from_window(0x4560, 1280, 720)
    register.assert_not_called()
    lib.obs_display_destroy.assert_not_called()


def test_cocoa_wrong_thread_release_preserves_resource(backend):
    lib, _ = backend
    display = module.Display.from_window(0x4560, 1280, 720)
    module._require_cocoa_main_thread.side_effect = RuntimeError("main thread")
    with pytest.raises(RuntimeError, match="main thread"):
        display.release()
    lib.obs_display_destroy.assert_not_called()
    assert display._ptr != ffi.NULL
    module._require_cocoa_main_thread.side_effect = None
    display.release()


@pytest.mark.parametrize("system, field", [("Windows", "hwnd"), ("Linux", "id")])
def test_existing_platform_window_routing(backend, monkeypatch, system, field):
    lib, init = backend
    monkeypatch.setattr(module.platform, "system", lambda: system)
    display = module.Display.from_window(0x4560, 1280, 720, x_display=0x7890)
    actual = getattr(init.window, field)
    assert (int(ffi.cast("uintptr_t", actual)) if field == "hwnd" else actual) == 0x4560
    module._require_cocoa_main_thread.assert_not_called()
    display.release()
    lib.obs_display_destroy.assert_called_once()


@pytest.mark.skipif(sys.platform != "darwin", reason="Cocoa thread API")
def test_native_main_thread_detection():
    # Use the actual native check, including when Python runs on a worker.
    # Access the original function even if other tests patched its module attr.
    check = module._require_cocoa_main_thread
    check()
    errors = []
    def worker():
        try:
            check()
        except RuntimeError as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1
