"""Native frame leases and scene callbacks use their declared C ABI directly."""
import pytest

from pylibobs import OBSContext, Scene
from pylibobs._ffi import ffi, get_lib

pytestmark = pytest.mark.integration


def test_atomic_scene_update_and_empty_frame_lease():
    with OBSContext():
        parent = Scene.create("atomic-parent")
        child = Scene.create("atomic-child")
        lib = get_lib()
        source = lib.obs_scene_get_source(child._ptr)
        # Scene sources have no asynchronous frame to lease. The getter still
        # accepts the real source argument and returns a typed null pointer.
        assert lib.obs_source_get_frame(source) == ffi.NULL
        received = []
        data = ffi.new_handle(received)

        @ffi.callback("obs_scene_atomic_update_func")
        def update(param, scene):
            ffi.from_handle(param).append(scene == parent._ptr)
            item = lib.obs_scene_add(scene, source)
            assert item != ffi.NULL

        lib.obs_scene_atomic_update(parent._ptr, update, data)
        assert received == [True]
        assert len(parent.items()) == 1
