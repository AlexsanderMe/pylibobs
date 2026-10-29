"""
Unit tests for the move to libobs 33 — declarations, struct layouts, plugin
discovery and module loading. No libobs needed.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pylibobs import _declarations, _declarations_auto
from pylibobs._ffi import ffi


def _real_declarations() -> str:
    parts = [v for k, v in vars(_declarations).items() if k.isupper() and isinstance(v, str)
             and k != "STUB_DECLS"]
    parts.append(_declarations_auto.AUTO_DECLS)
    return "\n".join(parts)


def _signature(name: str) -> str | None:
    m = re.search(rf"^[^\n/]*\b{name}\s*\([^;]*\);", _real_declarations(), re.M)
    return m.group(0).strip() if m else None


# ---------------------------------------------------------------- declarations
def test_monitoring_enabled_is_declared_with_bool():
    assert "bool enabled" in _signature("obs_source_set_monitoring_enabled")
    assert _signature("obs_source_get_monitoring_enabled").startswith("bool ")


def test_deprecated_monitoring_type_is_still_declared():
    # deprecated in 33, removal planned for 34 — old raw callers keep working
    assert _signature("obs_source_set_monitoring_type") is not None


@pytest.mark.parametrize("name", [
    "obs_add_core_module", "gs_image_file_init", "gs_image_file2_init",
    "gs_image_file3_init", "gs_image_file4_init", "gs_image_file_free",
])
def test_functions_removed_in_33_are_not_declared(name):
    assert _signature(name) is None
    assert name not in _declarations_auto.STUB_DECLS


def test_audio_hotkey_translations_take_six_strings():
    sig = _signature("obs_hotkeys_set_audio_hotkeys_translations")
    assert sig.count("const char *") == 6 and "monitor_on" in sig and "monitor_off" in sig


def test_obs_get_module_is_callable_not_a_stub():
    assert _signature("obs_get_module") is not None
    assert "obs_get_module(" not in _declarations_auto.STUB_DECLS


# ---------------------------------------------------------------- layouts
def test_gs_image_file_ex_is_a_value_struct():
    """libobs writes the whole struct into caller memory: an opaque pointer
    typedef would have callers allocate 8 bytes for 72."""
    assert ffi.sizeof("gs_image_file_ex_t") == 72
    assert ffi.offsetof("gs_image_file_ex_t", "internal") == 24
    assert ffi.offsetof("gs_image_file_ex_t", "alpha_mode") == 64


def test_runtime_module_structs():
    assert ffi.sizeof("struct obs_runtime_module_info") == 32
    assert ffi.offsetof("struct obs_runtime_module_info", "type") == 16
    assert ffi.offsetof("struct obs_runtime_module_info", "name") == 24
    assert ffi.sizeof("struct obs_module_failure_info") == 16


# ---------------------------------------------------------------- discovery
def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_flat_layout_skips_helper_libraries(tmp_path, monkeypatch):
    from pylibobs import _lib
    monkeypatch.setattr(_lib, "_lib_suffix", lambda: ".so")
    core = tmp_path / "obs-modules" / "core"
    for name in ("obs-ffmpeg.so", "image-source.so", "libcef.so", "libEGL.so"):
        _touch(core / name)
    found = _lib._layout_modules("flat", core, str(tmp_path / "data" / "%module%"))
    assert [m[0] for m in found] == ["image-source", "obs-ffmpeg"]
    assert found[1][2] == str(tmp_path / "data" / "obs-ffmpeg")


def test_windows_per_directory_layout(tmp_path, monkeypatch):
    from pylibobs import _lib
    monkeypatch.setattr(_lib, "_lib_suffix", lambda: ".dll")
    _touch(tmp_path / "core" / "obs-ffmpeg" / "obs-ffmpeg.dll")
    _touch(tmp_path / "core" / "win-dshow" / "win-dshow.dll")
    _touch(tmp_path / "core" / "empty-dir" / "readme.txt")      # no module binary
    found = _lib._layout_modules("per_dir", tmp_path / "core",
                                 str(tmp_path / "core" / "%module%" / "data"))
    assert found == [
        ("obs-ffmpeg", str(tmp_path / "core/obs-ffmpeg/obs-ffmpeg.dll"), str(tmp_path / "core/obs-ffmpeg/data")),
        ("win-dshow", str(tmp_path / "core/win-dshow/win-dshow.dll"), str(tmp_path / "core/win-dshow/data")),
    ]


def test_macos_plugin_bundles(tmp_path):
    from pylibobs import _lib
    _touch(tmp_path / "PlugIns" / "obs-ffmpeg.plugin" / "Contents" / "MacOS" / "obs-ffmpeg")
    found = _lib._layout_modules("macos", tmp_path / "PlugIns", "")
    assert found == [("obs-ffmpeg",
                      str(tmp_path / "PlugIns/obs-ffmpeg.plugin/Contents/MacOS/obs-ffmpeg"),
                      str(tmp_path / "PlugIns/obs-ffmpeg.plugin/Contents/Resources"))]


def test_bundled_obs33_layout_wins_and_qt_modules_are_dropped_from_system(tmp_path, monkeypatch):
    from pylibobs import _lib
    monkeypatch.setattr(_lib.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_lib, "_lib_suffix", lambda: ".so")
    libs = tmp_path / "_libs"
    lib_dir = libs / "linux" / "x86_64"
    _touch(lib_dir / "libobs.so.30")
    _touch(lib_dir / "obs-modules" / "core" / "obs-ffmpeg.so")
    _touch(lib_dir / "obs-plugins" / "old-module.so")             # legacy layout, lower priority
    monkeypatch.setattr(_lib, "_LIBS_DIR", libs)
    monkeypatch.setattr(_lib, "find_libobs", lambda: str(lib_dir / "libobs.so.30"))
    assert [m[0] for m in _lib.discover_modules()] == ["obs-ffmpeg"]
    assert [m[0] for m in _lib.get_bundled_modules()] == ["obs-ffmpeg"]

    # a system install: frontend plugins must never be loaded
    system = tmp_path / "usr" / "lib" / "obs-modules" / "core"
    for name in ("obs-ffmpeg.so", "frontend-tools.so", "obs-websocket.so"):
        _touch(system / name)
    monkeypatch.setattr(_lib, "_module_layouts",
                        lambda _d: [("flat", system, str(tmp_path / "%module%"), False)])
    assert [m[0] for m in _lib.discover_modules()] == ["obs-ffmpeg"]
    assert _lib.get_bundled_modules() == []


# ---------------------------------------------------------------- loading
@pytest.fixture()
def loading_ctx(monkeypatch):
    lib = MagicMock()
    lib.obs_get_module.return_value = None
    lib.obs_get_version.return_value = 33 << 24
    monkeypatch.setattr("pylibobs.context.get_lib", lambda: lib)
    monkeypatch.setattr("pylibobs.context._modules_in_added_paths", lambda: [])
    ffi_mod = MagicMock()
    ffi_mod.NULL = None
    monkeypatch.setattr("pylibobs.context.ffi", ffi_mod)
    from pylibobs.context import OBSContext
    return OBSContext(), lib, monkeypatch


def test_load_modules_opens_each_module_and_records_failures(loading_ctx):
    ctx, lib, monkeypatch = loading_ctx
    monkeypatch.setattr("pylibobs.context.discover_modules", lambda: [
        ("good", "/m/good.so", "/d/good"),
        ("missing", "/m/missing.so", "/d/missing"),
        ("bad-init", "/m/bad-init.so", "/d/bad-init"),
    ])
    lib.obs_open_module.side_effect = [0, -2, 0]           # MODULE_SUCCESS, MODULE_FILE_NOT_FOUND
    lib.obs_init_module.side_effect = [True, False]
    ctx.load_modules()
    assert ctx.failed_modules == [("missing", "file not found"),
                                  ("bad-init", "obs_module_load() returned false")]
    lib.obs_load_all_modules.assert_not_called()           # a no-op since OBS 33
    lib.obs_post_load_modules.assert_called_once()


def test_load_modules_warns_when_nothing_loads(loading_ctx):
    ctx, lib, monkeypatch = loading_ctx
    monkeypatch.setattr("pylibobs.context.discover_modules", lambda: [("x", "/m/x.so", "/d/x")])
    lib.obs_open_module.return_value = -4
    with pytest.warns(RuntimeWarning, match="no OBS module could be loaded"):
        ctx.load_modules()


def test_load_modules_warns_when_no_plugins_exist(loading_ctx):
    ctx, _lib_mock, monkeypatch = loading_ctx
    monkeypatch.setattr("pylibobs.context.discover_modules", lambda: [])
    with pytest.warns(RuntimeWarning, match="no OBS plugin modules found"):
        ctx.load_modules()


@pytest.mark.parametrize("system, binary, lookup", [
    ("Darwin", "/modules/obs-ffmpeg.dylib", b"obs-ffmpeg.dylib"),
    ("Darwin", "/modules/obs-ffmpeg.plugin/Contents/MacOS/obs-ffmpeg", b"obs-ffmpeg"),
    ("Linux", "/modules/obs-ffmpeg.so", b"obs-ffmpeg"),
    ("Windows", "C:/modules/obs-ffmpeg.dll", b"obs-ffmpeg"),
])
def test_loaded_modules_are_not_reopened(loading_ctx, system, binary, lookup):
    ctx, lib, monkeypatch = loading_ctx
    monkeypatch.setattr("pylibobs.context.platform.system", lambda: system)
    lib.obs_get_module.side_effect = lambda name: object() if name == lookup else None
    assert ctx._open_and_init_module("obs-ffmpeg", binary, "/data") is None
    lib.obs_open_module.assert_not_called()
    lib.obs_init_module.assert_not_called()


def test_modules_under_added_paths_are_loaded_once(loading_ctx):
    ctx, lib, monkeypatch = loading_ctx
    monkeypatch.setattr("pylibobs.context.discover_modules", lambda: [("obs-ffmpeg", "/b/obs-ffmpeg.so", "/d")])
    monkeypatch.setattr("pylibobs.context._modules_in_added_paths", lambda: [
        ("obs-ffmpeg", "/extra/obs-ffmpeg.so", "/x"),        # already bundled: skipped
        ("frontend-tools", "/extra/frontend-tools.so", "/x"),  # Qt frontend: never loaded
        ("my-plugin", "/extra/my-plugin.so", "/x/my-plugin"),
    ])
    lib.obs_open_module.return_value = 0
    lib.obs_init_module.return_value = True
    ctx.load_modules()
    opened = [c.args[1] for c in lib.obs_open_module.call_args_list]
    assert opened == [b"/b/obs-ffmpeg.so", b"/extra/my-plugin.so"]


def test_pre_33_libobs_without_found_modules_falls_back_to_its_own_scan(loading_ctx):
    ctx, lib, monkeypatch = loading_ctx
    lib.obs_get_version.return_value = (32 << 24) | (2 << 16) | 2
    monkeypatch.setattr("pylibobs.context.discover_modules", lambda: [])
    with pytest.warns(RuntimeWarning):
        ctx.load_modules()
    lib.obs_load_all_modules.assert_called_once()


def test_startup_names_the_monitoring_hotkeys(monkeypatch):
    lib = MagicMock()
    lib.obs_initialized.return_value = False
    lib.obs_startup.return_value = True
    lib.obs_get_version.return_value = 33 << 24
    monkeypatch.setattr("pylibobs.context.get_lib", lambda: lib)
    monkeypatch.setattr("pylibobs.context.get_obs_data_dir", lambda: None)
    monkeypatch.setattr("pylibobs.context.mark_alive", lambda: None)
    from pylibobs.context import OBSContext
    OBSContext().startup()
    args = lib.obs_hotkeys_set_audio_hotkeys_translations.call_args[0]
    assert len(args) == 6 and args[4:] == (b"Enable Monitoring", b"Disable Monitoring")


def test_startup_warns_on_another_libobs_major(monkeypatch):
    lib = MagicMock()
    lib.obs_initialized.return_value = False
    lib.obs_startup.return_value = True
    lib.obs_get_version.return_value = (32 << 24) | (2 << 16) | 2
    monkeypatch.setattr("pylibobs.context.get_lib", lambda: lib)
    monkeypatch.setattr("pylibobs.context.get_obs_data_dir", lambda: None)
    monkeypatch.setattr("pylibobs.context.mark_alive", lambda: None)
    ffi_mod = MagicMock()
    ffi_mod.string.return_value = b"32.2.2"
    monkeypatch.setattr("pylibobs.context.ffi", ffi_mod)
    from pylibobs.context import OBSContext
    with pytest.warns(RuntimeWarning, match="targets libobs 33.x but loaded 32.2.2"):
        OBSContext().startup()


def test_no_wrapper_uses_apis_deprecated_in_33():
    root = Path(__file__).resolve().parents[2] / "pylibobs"
    offenders = []
    for py in root.glob("*.py"):
        if py.name.startswith("_declarations"):
            continue
        text = py.read_text()
        # obs_load_all_modules is allowed only in load_modules' pre-33 fallback
        allowed = {"context.py": {"obs_load_all_modules"}}.get(py.name, set())
        for name in ("obs_load_all_modules", "obs_source_set_hidden", "obs_source_is_hidden",
                     "obs_source_set_monitoring_type", "obs_source_get_monitoring_type"):
            if name not in allowed and re.search(rf"lib\.{name}\(", text):
                offenders.append(f"{py.name}: {name}")
    assert not offenders, offenders
