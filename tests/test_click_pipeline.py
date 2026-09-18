"""Exercise the actual click-to-canvas pipeline, with only OBS IO mocked."""

from unittest.mock import MagicMock

import pytest

from tests.test_multiscreen import (
    DUAL_IDENTICAL, DUAL_RETINA_MIXED, DUAL_SIDE_BY_SIDE, SINGLE,
)


@pytest.fixture()
def pipeline(obs_script, monkeypatch):
    monkeypatch.setattr(obs_script, "_show_source", MagicMock())
    monkeypatch.setattr(obs_script, "_hide_source", MagicMock())
    monkeypatch.setattr(obs_script, "_get_capture_transform",
                        MagicMock(return_value=None))
    return obs_script


def assert_center(script, x, y):
    script._show_source.assert_called_once()
    _, _, left, top, size = script._show_source.call_args.args
    assert (left + size / 2, top + size / 2) == pytest.approx((x, y))


@pytest.mark.parametrize(
    "displays, point",
    [
        (DUAL_IDENTICAL, (960, 540)),
        (DUAL_IDENTICAL, (2880, 540)),
        (DUAL_SIDE_BY_SIDE, (960, 540)),
        (DUAL_SIDE_BY_SIDE, (3200, 720)),
        (DUAL_RETINA_MIXED, (840, 525)),
        (DUAL_RETINA_MIXED, (2640, 540)),
        ([dict(id=0, x=-1280, y=-200, w=1920, h=1080, retina_scale=1)],
         (-320, 340)),
        ([dict(id=0, x=0, y=0, w=1680, h=1050, retina_scale=2)],
         (840, 525)),
    ],
    ids=["primary", "secondary", "mixed_primary", "mixed_secondary",
         "retina_primary", "non_retina_secondary", "single_offset",
         "single_retina"],
)
def test_blank_selection_maps_clicked_monitor_not_virtual_desktop(
        pipeline, displays, point):
    pipeline._all_displays = displays
    pipeline._spawn_circle(*point, True, 999)
    assert_center(pipeline, 960, 540)


@pytest.mark.parametrize("displays", [SINGLE, DUAL_IDENTICAL])
def test_manual_monitor_dimensions_are_respected(pipeline, displays):
    pipeline._all_displays = displays
    pipeline._settings.update(override_monitor=True,
                              monitor_w=3840, monitor_h=2160)
    pipeline._spawn_circle(960, 540, True, 999)
    assert_center(pipeline, 480, 270)


def test_automatic_dimensions_ignore_stale_manual_values(pipeline):
    pipeline._all_displays = SINGLE
    pipeline._settings.update(override_monitor=False,
                              monitor_w=3840, monitor_h=2160)
    pipeline._spawn_circle(960, 540, True, 999)
    assert_center(pipeline, 960, 540)


def test_unknown_display_geometry_can_use_manual_dimensions(pipeline):
    pipeline._all_displays = []
    pipeline._settings.update(override_monitor=True,
                              monitor_w=2560, monitor_h=1440)
    pipeline._spawn_circle(1280, 720, True, 999)
    assert_center(pipeline, 960, 540)


def test_none_combo_label_uses_monitor_fallback(pipeline):
    pipeline._all_displays = DUAL_IDENTICAL
    pipeline._settings["capture_source"] = "(none)"
    pipeline._spawn_circle(2880, 540, True, 999)
    assert_center(pipeline, 960, 540)


@pytest.mark.parametrize(
    "displays, display_index, point",
    [(SINGLE, 0, (480, 270)),
     (DUAL_IDENTICAL, 0, (480, 270)),
     (DUAL_IDENTICAL, 1, (2400, 270))],
    ids=["single", "primary", "secondary"],
)
def test_all_mode_uses_resolved_capture_even_on_one_monitor(
        pipeline, mock_obs, displays, display_index, point):
    pipeline._all_displays = displays
    pipeline._multi_capture_mode = True
    pipeline._settings["capture_source"] = pipeline._ALL_CAPTURES_LABEL
    display = displays[display_index]
    pipeline._display_capture_map = {
        display["id"]: {"display": display, "source_name": "Capture"},
    }
    pipeline._get_capture_transform.return_value = {
        "capture_transform": (0.5, 0, 0, 0.5, 100, 50),
    }

    pipeline._spawn_circle(*point, True, 999)

    pipeline._get_capture_transform.assert_called_once_with(
        mock_obs._scene, "Capture")
    assert_center(pipeline, 340, 185)


@pytest.mark.parametrize("displays", [SINGLE, DUAL_IDENTICAL, []])
def test_all_mode_does_not_draw_for_unmapped_displays(pipeline, displays):
    pipeline._all_displays = displays
    pipeline._multi_capture_mode = True
    pipeline._settings["capture_source"] = pipeline._ALL_CAPTURES_LABEL
    pipeline._spawn_circle(480, 270, True, 999)
    pipeline._show_source.assert_not_called()
    assert pipeline._active_clicks == []


def test_single_capture_rejects_clicks_on_other_monitor(pipeline):
    pipeline._all_displays = DUAL_IDENTICAL
    pipeline._captured_display = DUAL_IDENTICAL[0]
    pipeline._settings["capture_source"] = "Primary capture"
    pipeline._spawn_circle(2400, 270, True, 999)
    pipeline._show_source.assert_not_called()
    pipeline._get_capture_transform.assert_not_called()


def test_selected_capture_with_unknown_monitor_does_not_guess_origin(pipeline):
    # Geometry synthesized after detection failure is not a verified origin.
    pipeline._all_displays = [
        dict(id=0, x=0, y=0, w=1920, h=1080, retina_scale=1),
    ]
    pipeline._settings["capture_source"] = "Unresolved capture"
    pipeline._get_capture_transform.return_value = {
        "capture_transform": (0.75, 0, 0, 0.75, 0, 0),
    }
    pipeline._spawn_circle(3200, 720, True, 999)
    pipeline._show_source.assert_not_called()
    pipeline._get_capture_transform.assert_not_called()


def test_unresolved_selected_monitor_logs_skipped_clicks(
        obs_script, mock_obs, monkeypatch):
    obs_script._settings["capture_source"] = "Unresolved capture"
    monkeypatch.setattr(obs_script, "_detect_all_displays", lambda: SINGLE)
    monkeypatch.setattr(obs_script, "_resolve_display_for_source", lambda name: None)
    obs_script._refresh_displays()
    assert any("skipped" in call.args[1]
               for call in mock_obs.script_log.call_args_list)


def test_single_capture_uses_display_origin_and_physical_retina_pixels(pipeline):
    display = dict(id=0, x=-1680, y=200, w=1680, h=1050, retina_scale=2)
    pipeline._all_displays = [display]
    pipeline._captured_display = display
    pipeline._settings["capture_source"] = "Retina capture"
    pipeline._get_capture_transform.return_value = {
        "crop_left": 200, "crop_top": 100,
        "capture_transform": (0.5, 0, 0, 0.5, 100, 50),
    }
    pipeline._spawn_circle(-1200, 470, True, 999)
    # Local (480,270) -> physical (960,540) -> crop (760,440)
    # -> transform (480,270).
    assert_center(pipeline, 480, 270)


@pytest.mark.parametrize("all_mode", [False, True])
def test_missing_selected_capture_does_not_guess_or_evict_circles(
        pipeline, mock_obs, all_mode):
    pipeline._all_displays = SINGLE
    pipeline._multi_capture_mode = all_mode
    pipeline._captured_display = None if all_mode else SINGLE[0]
    pipeline._settings["capture_source"] = (
        pipeline._ALL_CAPTURES_LABEL if all_mode else "Missing capture")
    pipeline._settings["max_circles"] = 1
    pipeline._display_capture_map = {
        1: {"display": SINGLE[0], "source_name": "Missing capture"},
    }
    pipeline._active_clicks[:] = [("__click_pop_L_0", 500)]

    pipeline._spawn_circle(480, 270, True, 999)

    pipeline._show_source.assert_not_called()
    pipeline._hide_source.assert_not_called()
    assert pipeline._active_clicks == [("__click_pop_L_0", 500)]
    mock_obs.obs_source_release.assert_called_once_with(mock_obs._scene_source)


def test_no_current_scene_does_not_allocate_a_circle(pipeline, mock_obs):
    mock_obs.obs_frontend_get_current_scene.return_value = None
    pipeline._spawn_circle(100, 200, True, 999)
    pipeline._show_source.assert_not_called()
    assert pipeline._active_clicks == []


def test_transform_error_releases_scene_reference(pipeline, mock_obs):
    pipeline._get_capture_transform.side_effect = RuntimeError("transform failed")
    with pytest.raises(RuntimeError, match="transform failed"):
        pipeline._spawn_circle(100, 200, True, 999)
    mock_obs.obs_source_release.assert_called_once_with(mock_obs._scene_source)
