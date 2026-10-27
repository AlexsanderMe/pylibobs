"""
Integration tests for the libobs 33 changes, against the real bundle:

* plugins load from the new obs-modules/core layout (obs_load_all_modules is a
  no-op in 33, so pylibobs loads them one by one)
* audio monitoring became on/off (obs_source_set/get_monitoring_enabled)
* audio sources gained monitor on/off hotkeys
* gs_image_file / 2 / 3 / 4 were replaced by gs_image_file_ex
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from pylibobs import OBSContext, OBSData, Source, enum_input_types
from pylibobs._ffi import ffi, get_lib
from pylibobs.persistence import load_source, save_source

pytestmark = pytest.mark.integration

ESSENTIAL_MODULES = ("obs-ffmpeg", "image-source", "obs-transitions", "obs-outputs", "obs-x264")


@pytest.fixture
def obs():
    with OBSContext() as ctx:
        ctx.set_video(320, 180, fps_num=30)
        ctx.set_audio()
        ctx.load_modules()
        yield ctx


def test_libobs_is_33(obs):
    assert get_lib().obs_get_version() >> 24 == 33


def test_essential_modules_load_from_the_33_layout(obs):
    lib = get_lib()
    for name in ESSENTIAL_MODULES:
        assert lib.obs_get_module(name.encode()) != ffi.NULL, f"{name} not loaded ({obs.failed_modules})"
    failed = {name for name, _reason in obs.failed_modules}
    assert not failed & set(ESSENTIAL_MODULES)
    types = enum_input_types()
    assert "ffmpeg_source" in types and "color_source_v3" in types


def test_monitoring_enabled_roundtrip(obs):
    src = Source.create("ffmpeg_source", "media", {})
    assert src.monitoring_enabled is False
    src.monitoring_enabled = True
    assert src.monitoring_enabled is True
    src.monitoring_enabled = False
    assert src.monitoring_enabled is False


def test_monitor_only_is_monitoring_plus_mute(obs):
    src = Source.create("ffmpeg_source", "media", {})
    src.set_monitor_only()
    assert (src.monitoring_enabled, src.muted) == (True, True)
    src.set_monitor_only(False)
    assert (src.monitoring_enabled, src.muted) == (False, False)


def test_saved_source_carries_both_monitoring_keys(obs):
    src = Source.create("ffmpeg_source", "media", {})
    src.monitoring_enabled = True
    saved = json.loads(save_source(src).to_json())
    assert saved["monitoring_enabled"] is True
    assert saved["monitoring_type"] != 0          # still written for older readers


def test_old_monitor_only_collection_loads_as_enabled(obs):
    """A 32.x collection saved with MONITOR_ONLY (1) and no monitoring_enabled
    key: libobs 33 turns it into "enabled" and does NOT mute it, so that audio
    now also reaches outputs. pylibobs documents this rather than hiding it."""
    old = OBSData.from_json(json.dumps({
        "id": "ffmpeg_source", "versioned_id": "ffmpeg_source", "name": "old",
        "monitoring_type": 1, "prev_ver": (32 << 24) | (2 << 16) | 2, "settings": {},
    }))
    src = load_source(old)
    assert src.monitoring_enabled is True
    assert src.muted is False


def test_audio_sources_get_named_monitoring_hotkeys(obs):
    src = Source.create("ffmpeg_source", "media", {})   # keep it alive: its hotkeys go with it
    found = {}

    @ffi.callback("bool(void *, obs_hotkey_id, obs_hotkey_t *)")
    def _collect(_data, _id, hotkey):
        name = ffi.string(get_lib().obs_hotkey_get_name(hotkey)).decode()
        if name.startswith("libobs.monitor-"):
            desc = get_lib().obs_hotkey_get_description(hotkey)
            found[name] = ffi.string(desc).decode() if desc != ffi.NULL else None
        return True

    get_lib().obs_enum_hotkeys(_collect, ffi.NULL)
    del src
    assert found == {"libobs.monitor-on": "Enable Monitoring",
                     "libobs.monitor-off": "Disable Monitoring"}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed to make the test image")
def test_gs_image_file_ex_loads_an_image(obs, tmp_path):
    png = tmp_path / "red.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x48",
                    "-frames:v", "1", str(png)], check=True, capture_output=True)
    lib = get_lib()
    image = ffi.new("gs_image_file_ex_t *")
    lib.gs_image_file_ex_init(image, str(png).encode(), 0)
    try:
        assert image.loaded and (image.cx, image.cy) == (64, 48)
        assert not image.is_animated_gif
        lib.obs_enter_graphics()
        try:
            lib.gs_image_file_ex_init_texture(image)
            assert image.texture != ffi.NULL
        finally:
            lib.obs_leave_graphics()
    finally:
        lib.obs_enter_graphics()
        lib.gs_image_file_ex_free(image)
        lib.obs_leave_graphics()
