"""
Integration tests for the libobs 32.2 API additions, against the real bundle:

* obs_source_get_dark_icon / obs_source_get_light_icon — themed custom icons
* OBS_ENCODER_CAP_MULTITRACK_DYN_BITRATE, via obs_get_encoder_caps /
  obs_encoder_get_caps (exposed as EncoderCaps)
"""

from __future__ import annotations

import pytest

from pylibobs import (
    EncoderCaps, OBSContext, VideoEncoder,
    enum_encoder_types, enum_source_types,
    get_encoder_caps, get_source_dark_icon, get_source_light_icon,
)
from pylibobs._ffi import get_lib

pytestmark = pytest.mark.integration


@pytest.fixture
def obs():
    with OBSContext() as ctx:
        ctx.set_video(320, 180, fps_num=30)
        ctx.set_audio()
        ctx.load_modules()
        yield ctx


def test_bundled_libobs_has_the_32_2_api(obs):
    ver = get_lib().obs_get_version()
    assert (ver >> 24, (ver >> 16) & 0xFF) >= (32, 2), f"libobs {ver >> 24}.{(ver >> 16) & 0xFF} predates 32.2"


def test_themed_icons_are_none_for_stock_sources(obs):
    """No stock plugin declares a custom icon, so libobs returns NULL for all."""
    types = enum_source_types()
    assert "ffmpeg_source" in types
    for source_id in types:
        assert get_source_dark_icon(source_id) is None, source_id
        assert get_source_light_icon(source_id) is None, source_id


def test_themed_icons_for_unknown_type_are_none(obs):
    assert get_source_dark_icon("no_such_source_type") is None
    assert get_source_light_icon("no_such_source_type") is None


def test_x264_caps(obs):
    # obs-x264.c: .caps = OBS_ENCODER_CAP_DYN_BITRATE | OBS_ENCODER_CAP_ROI
    assert get_encoder_caps("obs_x264") == EncoderCaps.DYN_BITRATE | EncoderCaps.ROI


def test_unknown_encoder_caps_is_none(obs):
    assert get_encoder_caps("no_such_encoder") == EncoderCaps.NONE


def test_encoder_instance_caps_match_its_type(obs):
    enc = VideoEncoder.create("obs_x264", "v", {"rate_control": "CRF", "crf": 23})
    assert enc.caps == get_encoder_caps("obs_x264")
    assert EncoderCaps.MULTITRACK_DYN_BITRATE not in enc.caps


def test_every_encoder_reports_only_known_caps(obs):
    """If libobs grows another cap bit, EncoderCaps must learn it too."""
    known = 0
    for flag in EncoderCaps:
        known |= flag
    for encoder_id in enum_encoder_types():
        raw = get_lib().obs_get_encoder_caps(encoder_id.encode())
        assert raw & ~known == 0, f"{encoder_id} reports unknown cap bits {raw & ~known:#x}"
