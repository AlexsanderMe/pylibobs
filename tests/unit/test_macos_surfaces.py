"""Surface lifecycle and render-state contracts without Cocoa or libobs."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylibobs import macos_surfaces as surfaces
from pylibobs._ffi import ffi

ENDPOINT = "org.pylibobs.surfaces.test"


@pytest.fixture
def backend(monkeypatch):
    native = Mock()
    native.pys_presenter_create.return_value = 101
    native.pys_presenter_poll.return_value = 1
    native.pys_producer_create.return_value = 202
    native.pys_producer_publish.return_value = 1
    lib = Mock()
    lib.gs_texrender_create.return_value = ffi.cast("gs_texrender_t *", 0x1230)
    lib.gs_texrender_get_texture.return_value = ffi.cast("gs_texture_t *", 0x4560)
    lib.gs_texrender_begin.return_value = True
    obs_dll = Mock()
    obs_dll.gs_texture_get_obj.return_value = (ctypes.c_uint * 1)(17)
    monkeypatch.setattr(surfaces, "_native", lambda: native)
    monkeypatch.setattr(surfaces, "_main_thread", Mock())
    monkeypatch.setattr(surfaces.ctypes, "CDLL", lambda _: obs_dll)
    monkeypatch.setattr("pylibobs._ffi.get_lib", lambda: lib)
    monkeypatch.setattr("pylibobs._lib.find_libobs", lambda: "unused")
    return SimpleNamespace(native=native, lib=lib, dll=obs_dll)


def test_presenter_counts_presented_frames_and_closes_once(backend):
    backend.native.pys_presenter_poll.side_effect = [1, 0, 1]
    with surfaces.SurfacePresenter(0x1230) as presenter:
        assert [presenter.poll() for _ in range(3)] == [True, False, True]
        assert presenter.frames_presented == 2
    presenter.close()
    assert presenter.poll() is False
    backend.native.pys_presenter_close.assert_called_once_with(101)
    assert backend.native.pys_presenter_poll.call_count == 3


def test_frame_latency_is_reported_without_pixel_readback_and_resets_on_close(backend):
    backend.native.pys_presenter_latency_ns.return_value = 12_500_000
    with surfaces.SurfacePresenter(0x1230) as presenter:
        assert presenter.last_frame_latency_ms == 12.5
    assert presenter.last_frame_latency_ms == 0


def test_presenter_failure_does_not_count_frame_and_can_close(backend):
    backend.native.pys_presenter_poll.return_value = -1
    with surfaces.SurfacePresenter(0x1230) as presenter:
        with pytest.raises(RuntimeError, match="presentation failed"):
            presenter.poll()
        assert presenter.frames_presented == 0
    backend.native.pys_presenter_close.assert_called_once()


@pytest.mark.parametrize("address", [None, True, 0, -1, "123"])
def test_presenter_rejects_invalid_handles_without_native_creation(backend, address):
    with pytest.raises(ValueError):
        surfaces.SurfacePresenter(address)
    backend.native.pys_presenter_create.assert_not_called()


def test_presenter_creation_failure(backend):
    backend.native.pys_presenter_create.return_value = None
    with pytest.raises(RuntimeError, match="create"):
        surfaces.SurfacePresenter(0x1230)
    backend.native.pys_presenter_close.assert_not_called()


@pytest.mark.parametrize("endpoint,width,height", [
    ("foreign.endpoint", 320, 180), (ENDPOINT, 0, 180),
    (ENDPOINT, 320, -1), (ENDPOINT, True, 180),
    (ENDPOINT, 16385, 180), (ENDPOINT, 320, 1.5),
])
def test_producer_invalid_configuration_allocates_nothing(backend, endpoint, width, height):
    with pytest.raises(ValueError):
        surfaces.SurfaceProducer(endpoint, width, height)
    backend.native.pys_producer_create.assert_not_called()
    backend.lib.obs_enter_graphics.assert_not_called()


def test_producer_tracks_backpressure_and_restores_render_state(backend):
    backend.native.pys_producer_publish.side_effect = [1, 0, 1]
    source = ffi.cast("obs_source_t *", 0x7890)
    with surfaces.SurfaceProducer(ENDPOINT, 320, 180) as producer:
        backend.lib.reset_mock()
        assert [producer.render(source, canvas_width=640, canvas_height=360)
                for _ in range(3)] == [True, False, True]
        assert (producer.frames_published, producer.frames_dropped) == (2, 1)
        assert backend.lib.gs_blend_state_push.call_count == backend.lib.gs_blend_state_pop.call_count == 3
        assert backend.lib.gs_texrender_begin.call_count == backend.lib.gs_texrender_end.call_count == 3
        assert backend.lib.obs_enter_graphics.call_count == backend.lib.obs_leave_graphics.call_count == 3
    backend.native.pys_producer_close.assert_called_once()
    backend.lib.gs_texrender_destroy.assert_called_once()
    backend.lib.reset_mock()
    producer.close()
    assert producer.render(source, canvas_width=640, canvas_height=360) is False
    backend.lib.obs_enter_graphics.assert_not_called()


def test_producer_render_exception_unwinds_blend_texture_and_context(backend):
    backend.lib.obs_source_video_render.side_effect = RuntimeError("source failed")
    with surfaces.SurfaceProducer(ENDPOINT, 320, 180) as producer:
        backend.lib.reset_mock()
        with pytest.raises(RuntimeError, match="source failed"):
            producer.render(ffi.cast("obs_source_t *", 1), canvas_width=320, canvas_height=180)
        backend.lib.gs_blend_state_pop.assert_called_once()
        backend.lib.gs_texrender_end.assert_called_once()
        backend.lib.obs_leave_graphics.assert_called_once()
        backend.native.pys_producer_publish.assert_not_called()


def test_producer_disconnection_preserves_cleanup(backend):
    backend.native.pys_producer_publish.return_value = -1
    with surfaces.SurfaceProducer(ENDPOINT, 320, 180) as producer:
        with pytest.raises(ConnectionError):
            producer.render(ffi.cast("obs_source_t *", 1), canvas_width=320, canvas_height=180)
        assert (producer.frames_published, producer.frames_dropped) == (0, 0)
    backend.native.pys_producer_close.assert_called_once()


@pytest.mark.parametrize("failure", ["connection", "texture"])
def test_producer_construction_rollback_balances_graphics_context(backend, failure):
    if failure == "connection":
        backend.native.pys_producer_create.return_value = None
    else:
        backend.lib.gs_texrender_create.return_value = ffi.NULL
    with pytest.raises(RuntimeError):
        surfaces.SurfaceProducer(ENDPOINT, 320, 180)
    assert backend.lib.obs_enter_graphics.call_count == backend.lib.obs_leave_graphics.call_count
    assert backend.native.pys_producer_close.call_count == (failure == "texture")
    backend.lib.gs_texrender_destroy.assert_not_called()


def test_render_veto_does_not_allocate_or_publish(backend):
    with surfaces.SurfaceProducer(ENDPOINT, 320, 180) as producer:
        backend.lib.reset_mock()
        assert not producer.render(ffi.cast("obs_source_t *", 1), canvas_width=320,
                                   canvas_height=180, before_render=lambda: False)
        backend.lib.gs_texrender_begin.assert_not_called()
        backend.lib.obs_leave_graphics.assert_called_once()
        backend.native.pys_producer_publish.assert_not_called()


def test_platform_rejection_precedes_dynamic_library_loading(monkeypatch):
    monkeypatch.setattr(surfaces.platform, "system", lambda: "Linux")
    load = Mock(side_effect=AssertionError("must not load Cocoa"))
    monkeypatch.setattr(surfaces.ctypes, "CDLL", load)
    for operation in (surfaces._native, surfaces._main_thread):
        with pytest.raises(OSError):
            operation()
    load.assert_not_called()


def test_native_worker_thread_is_rejected(monkeypatch):
    monkeypatch.setattr(surfaces.platform, "system", lambda: "Darwin")
    library = Mock()
    library.pthread_main_np.return_value = 0
    monkeypatch.setattr(surfaces.ctypes, "CDLL", lambda _: library)
    with pytest.raises(RuntimeError, match="main thread"):
        surfaces._main_thread()


@pytest.mark.parametrize("source", [None, ffi.NULL])
def test_missing_source_publishes_opaque_black_without_rendering_stale_pixels(backend, source):
    with surfaces.SurfaceProducer(ENDPOINT, 320, 180) as producer:
        backend.lib.reset_mock()
        assert producer.render(source, canvas_width=320, canvas_height=180)
        color = backend.lib.gs_clear.call_args.args[1]
        assert (color.x, color.y, color.z, color.w) == (0, 0, 0, 1)
        backend.lib.obs_source_video_render.assert_not_called()
        backend.native.pys_producer_publish.assert_called_once()
        assert producer.frames_published == 1


def test_close_does_not_invert_graphics_lock_against_render_callback(backend):
    import threading

    producer = surfaces.SurfaceProducer(ENDPOINT, 320, 180)
    graphics = threading.RLock()
    rendering = threading.Event()
    closing = threading.Event()
    failures = []

    def enter_graphics():
        if threading.current_thread().name == "surface-close":
            closing.set()
        # A bounded acquisition exposes inversion without leaving hung threads
        # behind when the regression fires.
        if not graphics.acquire(timeout=0.5):
            raise RuntimeError("graphics and producer lock order inverted")

    backend.lib.obs_enter_graphics.side_effect = enter_graphics
    backend.lib.obs_leave_graphics.side_effect = graphics.release

    def callback():
        try:
            with graphics:
                rendering.set()
                assert closing.wait(timeout=2)
                producer.render(ffi.cast("obs_source_t *", 1), canvas_width=320, canvas_height=180)
        except BaseException as exc:
            failures.append(exc)

    def close():
        try:
            producer.close()
        except BaseException as exc:
            failures.append(exc)

    render_thread = threading.Thread(target=callback, name="surface-render")
    close_thread = threading.Thread(target=close, name="surface-close")
    render_thread.start()
    assert rendering.wait(timeout=2)
    close_thread.start()
    render_thread.join(timeout=3)
    close_thread.join(timeout=3)
    producer.close()
    assert not render_thread.is_alive() and not close_thread.is_alive()
    assert not failures, failures


def test_missing_helper_explains_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setattr(surfaces.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(surfaces, "__file__", str(tmp_path / "macos_surfaces.py"))
    load = Mock(side_effect=AssertionError("must not load a missing helper"))
    monkeypatch.setattr(surfaces.ctypes, "CDLL", load)
    with pytest.raises(OSError, match="xcode-select --install"):
        surfaces._native()
    load.assert_not_called()
