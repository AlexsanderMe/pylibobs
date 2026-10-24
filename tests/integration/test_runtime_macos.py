"""Exercise the packaged Cocoa runtime in a fresh process, without an OBS.app host."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="Cocoa runtime")
def test_bundled_framework_video_and_native_modules():
    from pylibobs._lib import _bundled_path
    if not _bundled_path():
        pytest.skip("Build the bundled runtime with scripts/fetch_libs.py first")
    environment = dict(os.environ)
    for key in ("LIBOBS_PATH", "OBS_DATA_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        environment.pop(key, None)
    script = '''
import gc
import tempfile
from pylibobs import OBSContext, Source, enum_source_types
from pylibobs._ffi import ffi, get_lib
from pylibobs._lib import find_libobs, get_bundled_modules
assert '/libobs.framework/' in find_libobs()
modules = {name for name, _, _ in get_bundled_modules()}
assert {'mac-avcapture', 'image-source', 'obs-transitions', 'obs-ffmpeg'} <= modules
with tempfile.TemporaryDirectory() as config:
    with OBSContext(module_config_path=config) as context:
        context.set_video(width=320, height=180, fps_num=30000, fps_den=1001)
        gc.collect()
        context.set_video(width=640, height=360)
        context.set_audio()
        context.load_modules()
        assert 'macos-avcapture' in enum_source_types()
        # Runtime loading must not implicitly open a physical/default camera.
        # Capture and permission behavior need explicitly selected hardware.
        for kind in ('image_source', 'color_source_v3', 'ffmpeg_source'):
            source = Source.create(kind, 'runtime-' + kind)
            assert get_lib().obs_source_get_output_flags(source._ptr) & 1, kind
            source.release()
print('native runtime shutdown complete')
'''
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native runtime shutdown complete" in result.stdout
