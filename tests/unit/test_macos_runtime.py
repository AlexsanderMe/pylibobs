"""macOS runtime layout and CFFI ownership regressions (no native runtime)."""
from __future__ import annotations

import gc
import runpy
import weakref
from pathlib import Path
from unittest.mock import Mock

import pytest

from pylibobs import _lib
from pylibobs._ffi import ffi
from pylibobs.context import OBSContext


def test_framework_identity_and_bundled_resource_lookup(monkeypatch, tmp_path):
    root = tmp_path / "macos" / "x86_64"
    executable = root / "Frameworks/libobs.framework/Versions/A/libobs"
    executable.parent.mkdir(parents=True)
    executable.touch()
    # Old flattened artifact must not take precedence over the bundle identity.
    (root / "Frameworks/libobs.dylib").touch()
    graphics = root / "Frameworks/libobs-opengl.dylib"
    graphics.touch()
    plugins = root / "obs-plugins"
    plugins.mkdir()
    (plugins / "image-source.dylib").touch()
    data = root / "data"
    (data / "libobs").mkdir(parents=True)
    monkeypatch.setattr(_lib, "_LIBS_DIR", tmp_path)
    monkeypatch.setattr(_lib.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_lib.platform, "machine", lambda: "x86_64")
    monkeypatch.delenv("LIBOBS_PATH", raising=False)
    monkeypatch.delenv("OBS_DATA_PATH", raising=False)
    assert _lib.find_libobs() == str(executable)
    assert _lib.get_obs_data_dir() == str(data)
    assert _lib.get_graphics_module() == str(graphics)
    assert _lib.get_bundled_modules() == [
        ("image-source", str(plugins / "image-source.dylib"), str(data / "obs-plugins/image-source"))
    ]


def test_video_graphics_owner_survives_native_call_and_gc(monkeypatch):
    import pylibobs.context as module
    allocations = []

    class TrackingFFI:
        def new(self, ctype, *args):
            result = ffi.new(ctype, *args)
            if ctype == "char[]":
                allocations.append(weakref.ref(result))
            return result

    lib = Mock()
    def reset(video):
        gc.collect()
        assert allocations[-1]() is not None, "graphics_module allocation died before obs_reset_video"
        assert ffi.string(video.graphics_module) == b"test-graphics"
        return 0
    lib.obs_reset_video.side_effect = reset
    monkeypatch.setattr(module, "get_lib", lambda: lib)
    monkeypatch.setattr(module, "ffi", TrackingFFI())
    context = OBSContext()
    context.set_video(graphics_module="test-graphics")
    gc.collect()
    assert allocations[-1]() is not None, "libobs retains graphics_module until shutdown"


def test_frame_rate_struct_supports_rational_values():
    fps = ffi.new("struct media_frames_per_second *", {"numerator": 30000, "denominator": 1001})
    assert fps.numerator / fps.denominator == pytest.approx(29.97002997)
    assert ffi.sizeof(fps[0]) == 8


def test_native_task_callbacks_are_callable_without_casts():
    seen = []
    @ffi.callback("obs_task_t")
    def task(param):
        seen.append(int(ffi.cast("uintptr_t", param)))
    @ffi.callback("obs_task_handler_t")
    def dispatch(callback, param, wait):
        callback(param)
    dispatch(task, ffi.cast("void *", 42), True)
    assert seen == [42]


def test_default_graphics_name_needs_no_installed_runtime(monkeypatch):
    monkeypatch.setattr(_lib.platform, "system", lambda: "Darwin")
    def missing():
        raise ImportError("No runtime")
    monkeypatch.setattr(_lib, "find_libobs", missing)
    assert _lib.get_graphics_module() == "libobs-opengl"


@pytest.fixture
def wheel_build(monkeypatch, tmp_path):
    from setuptools.dist import Distribution

    setup = Mock()
    monkeypatch.setattr("setuptools.setup", setup)
    runpy.run_path(str(Path(__file__).resolve().parents[2] / "setup.py"))
    command_type = setup.call_args.kwargs["cmdclass"]["bdist_wheel"]
    command = command_type(Distribution())
    build = Mock()
    monkeypatch.setattr(command_type.__mro__[1], "run", build)
    monkeypatch.setitem(command.run.__globals__, "__file__", str(tmp_path / "setup.py"))
    monkeypatch.delenv("PYLIBOBS_ALLOW_SYSTEM_LIBOBS", raising=False)
    return command, build, tmp_path / "pylibobs/_libs"


@pytest.mark.parametrize("relative", [
    "macos/arm64/Frameworks/libobs.framework/Versions/A/libobs",
    "macos/x86_64/Frameworks/libobs.framework/Versions/A/libobs",
    "macos/arm64/Frameworks/libobs.dylib",
    "macos/arm64/libobs.dylib",
    "windows/x86_64/obs.dll",
    "linux/x86_64/libobs.so.0",
])
def test_wheel_build_accepts_a_bundled_runtime(wheel_build, relative):
    command, build, libs = wheel_build
    executable = libs / relative
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundled runtime")
    command.run()
    build.assert_called_once()


@pytest.mark.parametrize("relative", [None, "macos/arm64/Frameworks/libobs.dylib"])
def test_wheel_build_rejects_missing_runtime_or_directory(wheel_build, relative):
    command, build, libs = wheel_build
    if relative is not None:
        (libs / relative).mkdir(parents=True)
    with pytest.raises(SystemExit, match="no bundled libobs"):
        command.run()
    build.assert_not_called()


def test_wheel_build_allows_explicit_system_runtime(wheel_build, monkeypatch):
    command, build, _libs = wheel_build
    monkeypatch.setenv("PYLIBOBS_ALLOW_SYSTEM_LIBOBS", "1")
    command.run()
    build.assert_called_once()
