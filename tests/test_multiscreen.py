"""Tests for multi-screen support: display hit-testing and coordinate mapping."""

from unittest.mock import Mock

import pytest
from click_pop_core import find_display_for_point, map_coords


# ---------------------------------------------------------------------------
# Display layouts used across tests
# ---------------------------------------------------------------------------

DUAL_SIDE_BY_SIDE = [
    {"id": 1, "x": 0, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
    {"id": 2, "x": 1920, "y": 0, "w": 2560, "h": 1440, "retina_scale": 1.0},
]

DUAL_IDENTICAL = [
    {"id": 1, "x": 0, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
    {"id": 2, "x": 1920, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
]

DUAL_VERTICAL = [
    {"id": 1, "x": 0, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
    {"id": 2, "x": 0, "y": 1080, "w": 1920, "h": 1080, "retina_scale": 1.0},
]

DUAL_RETINA_MIXED = [
    {"id": 1, "x": 0, "y": 0, "w": 1680, "h": 1050, "retina_scale": 2.0},
    {"id": 2, "x": 1680, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
]

TRIPLE_L_SHAPE = [
    {"id": 1, "x": 0, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
    {"id": 2, "x": 1920, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
    {"id": 3, "x": 0, "y": 1080, "w": 1920, "h": 1080, "retina_scale": 1.0},
]

SINGLE = [
    {"id": 1, "x": 0, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
]

# Windows DPI-scaled dual-monitor layout in *physical* pixel coordinates.
# Primary: 3840x2160 panel at 150 % → logical 2560x1440, physical 3840x2160.
# Secondary: 1920x1080 at 100 % → starts at physical x=3840.
# After the DPI fix, _detect_displays_win32 returns physical-pixel rects
# which is what pynput reports, so find_display_for_point must work with these.
DUAL_DPI_PHYSICAL = [
    {"id": 1, "x": 0, "y": 0, "w": 3840, "h": 2160, "retina_scale": 1.0},
    {"id": 2, "x": 3840, "y": 0, "w": 1920, "h": 1080, "retina_scale": 1.0},
]


# ---------------------------------------------------------------------------
# find_display_for_point tests
# ---------------------------------------------------------------------------

class TestFindDisplayForPoint:
    """Test that clicks are correctly attributed to the right display."""

    def test_click_on_primary(self):
        d = find_display_for_point(100, 200, DUAL_SIDE_BY_SIDE)
        assert d is DUAL_SIDE_BY_SIDE[0]

    def test_click_on_secondary_right(self):
        d = find_display_for_point(2000, 500, DUAL_SIDE_BY_SIDE)
        assert d is DUAL_SIDE_BY_SIDE[1]

    def test_click_on_secondary_below(self):
        d = find_display_for_point(500, 1200, DUAL_VERTICAL)
        assert d is DUAL_VERTICAL[1]

    def test_click_at_boundary_between_monitors(self):
        """x=1920 is the first pixel of the secondary monitor."""
        d = find_display_for_point(1920, 500, DUAL_SIDE_BY_SIDE)
        assert d is DUAL_SIDE_BY_SIDE[1]

    def test_click_at_last_pixel_primary(self):
        """x=1919 is the last pixel of the primary monitor."""
        d = find_display_for_point(1919, 500, DUAL_SIDE_BY_SIDE)
        assert d is DUAL_SIDE_BY_SIDE[0]

    def test_click_in_dead_zone(self):
        """The lower-right corner of the L-shaped layout has no monitor."""
        d = find_display_for_point(1920, 1200, TRIPLE_L_SHAPE)
        assert d is None

    def test_click_outside_all_monitors(self):
        d = find_display_for_point(-10, -10, DUAL_SIDE_BY_SIDE)
        assert d is None

    def test_single_monitor_center(self):
        d = find_display_for_point(960, 540, SINGLE)
        assert d is SINGLE[0]

    def test_single_monitor_origin(self):
        d = find_display_for_point(0, 0, SINGLE)
        assert d is SINGLE[0]

    def test_empty_display_list(self):
        d = find_display_for_point(100, 100, [])
        assert d is None

    def test_triple_monitor_third_display(self):
        d = find_display_for_point(500, 1500, TRIPLE_L_SHAPE)
        assert d is TRIPLE_L_SHAPE[2]

    def test_click_at_secondary_origin(self):
        d = find_display_for_point(1920, 0, DUAL_SIDE_BY_SIDE)
        assert d is DUAL_SIDE_BY_SIDE[1]

    def test_click_at_secondary_far_edge(self):
        """Last pixel of secondary: (1920+2560-1, 1440-1) = (4479, 1439)."""
        d = find_display_for_point(4479, 1439, DUAL_SIDE_BY_SIDE)
        assert d is DUAL_SIDE_BY_SIDE[1]

    def test_click_just_past_secondary(self):
        """One pixel beyond secondary: x=4480."""
        d = find_display_for_point(4480, 0, DUAL_SIDE_BY_SIDE)
        assert d is None


# ---------------------------------------------------------------------------
# Pure coordinate mapping tests with display-local input
# ---------------------------------------------------------------------------

class TestMultiScreenCoordMapping:
    """Check map_coords with logical/physical pixel and monitor-size inputs."""

    def test_click_on_primary_1080p_canvas(self):
        """Click at center of primary display, 1:1 canvas."""
        display = DUAL_SIDE_BY_SIDE[0]
        gx, gy = 960, 540  # center of primary

        local_x = gx - display["x"]
        local_y = gy - display["y"]
        retina = display["retina_scale"]

        result = map_coords(
            local_x * retina, local_y * retina,
            1920, 1080,
            display["w"] * retina, display["h"] * retina,
            80,
        )
        assert result == pytest.approx((920.0, 500.0))

    def test_click_on_secondary_1440p_with_1080p_canvas(self):
        """Click at center of secondary (2560x1440) with a 1920x1080 canvas."""
        display = DUAL_SIDE_BY_SIDE[1]
        gx, gy = 1920 + 1280, 720  # center of secondary in global coords

        local_x = gx - display["x"]  # 1280
        local_y = gy - display["y"]  # 720

        # canvas is 1920x1080, monitor is 2560x1440
        result = map_coords(
            local_x, local_y,
            1920, 1080,
            2560, 1440,
            80,
        )
        # scale_x = 1920/2560 = 0.75, scale_y = 1080/1440 = 0.75
        # obs_x = 1280 * 0.75 - 40 = 920
        # obs_y = 720 * 0.75 - 40 = 500
        assert result == pytest.approx((920.0, 500.0))

    def test_click_on_retina_primary(self):
        """Click at center of Retina display (2x), 1080p canvas."""
        display = DUAL_RETINA_MIXED[0]
        gx, gy = 840, 525  # center of 1680x1050 logical

        local_x = gx - display["x"]
        local_y = gy - display["y"]
        retina = display["retina_scale"]  # 2.0

        phys_x = local_x * retina  # 1680
        phys_y = local_y * retina  # 1050
        phys_mon_w = display["w"] * retina  # 3360
        phys_mon_h = display["h"] * retina  # 2100

        result = map_coords(phys_x, phys_y, 1920, 1080, phys_mon_w, phys_mon_h, 80)
        # scale_x = 1920/3360 ≈ 0.5714
        # obs_x = 1680 * 0.5714 - 40 ≈ 920
        assert result == pytest.approx((920.0, 500.0), abs=1.0)

    def test_click_on_non_retina_secondary(self):
        """Click at center of non-Retina secondary next to Retina primary."""
        display = DUAL_RETINA_MIXED[1]
        gx, gy = 1680 + 960, 540  # center of secondary in global coords

        local_x = gx - display["x"]  # 960
        local_y = gy - display["y"]  # 540
        retina = display["retina_scale"]  # 1.0

        result = map_coords(
            local_x * retina, local_y * retina,
            1920, 1080,
            display["w"] * retina, display["h"] * retina,
            80,
        )
        assert result == pytest.approx((920.0, 500.0))

    def test_click_on_secondary_with_top_left_origin(self):
        """Secondary at (0, 1080) — click at its origin should map to canvas origin."""
        display = DUAL_VERTICAL[1]
        gx, gy = 0, 1080  # top-left of secondary

        local_x = gx - display["x"]  # 0
        local_y = gy - display["y"]  # 0

        result = map_coords(local_x, local_y, 1920, 1080, 1920, 1080, 80)
        # obs_x = 0 * 1.0 - 40 = -40
        assert result == pytest.approx((-40.0, -40.0))

    def test_click_bottom_right_of_secondary(self):
        """Click at bottom-right of secondary display."""
        display = DUAL_SIDE_BY_SIDE[1]
        gx, gy = 1920 + 2559, 1439  # last pixel

        local_x = gx - display["x"]  # 2559
        local_y = gy - display["y"]  # 1439

        result = map_coords(local_x, local_y, 1920, 1080, 2560, 1440, 80)
        # scale = 0.75
        # obs_x = 2559 * 0.75 - 40 = 1879.25
        # obs_y = 1439 * 0.75 - 40 = 1039.25
        assert result == pytest.approx((1879.25, 1039.25))


# ---------------------------------------------------------------------------
# Windows DPI scaling regression tests
# ---------------------------------------------------------------------------

class TestDpiScalingRegression:
    """Verify that physical-pixel display rects (post-DPI fix) work correctly.

    After the DPI fix, _detect_displays_win32() returns physical-pixel
    coordinates which match what pynput's WH_MOUSE_LL hook reports.
    These tests confirm that find_display_for_point and map_coords handle
    these physical-pixel values correctly.
    """

    def test_click_center_primary_4k_display(self):
        """Click at center of a 4K primary display (physical pixels)."""
        d = find_display_for_point(1920, 1080, DUAL_DPI_PHYSICAL)
        assert d is DUAL_DPI_PHYSICAL[0]

    def test_click_on_secondary_after_4k_primary(self):
        """Click on secondary display whose origin is at physical x=3840."""
        d = find_display_for_point(4000, 500, DUAL_DPI_PHYSICAL)
        assert d is DUAL_DPI_PHYSICAL[1]

    def test_click_at_boundary_4k_to_secondary(self):
        """x=3840 is the first pixel of the secondary display."""
        d = find_display_for_point(3840, 0, DUAL_DPI_PHYSICAL)
        assert d is DUAL_DPI_PHYSICAL[1]

    def test_click_last_pixel_4k_primary(self):
        """x=3839 is the last pixel of the 4K primary."""
        d = find_display_for_point(3839, 500, DUAL_DPI_PHYSICAL)
        assert d is DUAL_DPI_PHYSICAL[0]

    def test_map_coords_center_of_4k_display(self):
        """Map center of physical 3840x2160 to a 1920x1080 canvas."""
        display = DUAL_DPI_PHYSICAL[0]
        gx, gy = 1920, 1080  # center of 3840x2160

        local_x = gx - display["x"]
        local_y = gy - display["y"]

        result = map_coords(
            local_x, local_y,
            1920, 1080,
            display["w"], display["h"],
            80,
        )
        # scale_x = 1920/3840 = 0.5, scale_y = 1080/2160 = 0.5
        # obs_x = 1920 * 0.5 - 40 = 920
        # obs_y = 1080 * 0.5 - 40 = 500
        assert result == pytest.approx((920.0, 500.0))

    def test_map_coords_secondary_after_4k(self):
        """Map center of secondary 1920x1080 (origin at physical x=3840)."""
        display = DUAL_DPI_PHYSICAL[1]
        gx, gy = 3840 + 960, 540  # center of secondary

        local_x = gx - display["x"]  # 960
        local_y = gy - display["y"]  # 540

        result = map_coords(
            local_x, local_y,
            1920, 1080,
            display["w"], display["h"],
            80,
        )
        # 1:1 mapping (1920x1080 → 1920x1080)
        # obs_x = 960 * 1.0 - 40 = 920
        # obs_y = 540 * 1.0 - 40 = 500
        assert result == pytest.approx((920.0, 500.0))


# ---------------------------------------------------------------------------
# Additional routing cases through the actual click pipeline
# ---------------------------------------------------------------------------

@pytest.fixture
def routing_script(obs_script, monkeypatch):
    monkeypatch.setattr(obs_script, "_show_source", Mock())
    monkeypatch.setattr(obs_script, "_hide_source", Mock())
    monkeypatch.setattr(obs_script, "_get_capture_transform", Mock(return_value={}))
    return obs_script


@pytest.mark.parametrize("point,expected_source,expected_center", [
    ((500, 500), "Capture primary", (350, 300)),
    ((2420, 500), "Capture right", (1250, 300)),
    ((500, 1580), "Capture below", (175, 825)),
])
def test_all_mode_routes_each_monitor_to_its_own_transform(
        routing_script, mock_obs, point, expected_source, expected_center):
    routing_script._all_displays = TRIPLE_L_SHAPE
    routing_script._multi_capture_mode = True
    routing_script._settings["capture_source"] = routing_script._ALL_CAPTURES_LABEL
    routing_script._display_capture_map = {
        1: {"display": TRIPLE_L_SHAPE[0], "source_name": "Capture primary"},
        2: {"display": TRIPLE_L_SHAPE[1], "source_name": "Capture right"},
        3: {"display": TRIPLE_L_SHAPE[2], "source_name": "Capture below"},
    }
    transforms = {
        "Capture primary": (0.5, 0, 0, 0.5, 100, 50),
        "Capture right": (0.5, 0, 0, 0.5, 1000, 50),
        "Capture below": (0.25, 0, 0, 0.25, 50, 700),
    }
    routing_script._get_capture_transform.side_effect = lambda scene, name: {
        "capture_transform": transforms[name],
    }

    routing_script._spawn_circle(*point, True, 999)

    routing_script._get_capture_transform.assert_called_once_with(
        mock_obs._scene, expected_source)
    routing_script._show_source.assert_called_once()
    _, _, x, y, size = routing_script._show_source.call_args.args
    assert (x + size / 2, y + size / 2) == pytest.approx(expected_center)


@pytest.mark.parametrize("all_mode", [False, True])
def test_click_in_monitor_gap_does_not_draw_or_evict(routing_script, mock_obs, all_mode):
    routing_script._all_displays = TRIPLE_L_SHAPE
    routing_script._captured_display = TRIPLE_L_SHAPE[0]
    routing_script._multi_capture_mode = all_mode
    routing_script._settings["capture_source"] = (
        routing_script._ALL_CAPTURES_LABEL if all_mode else "Capture primary")
    routing_script._display_capture_map = {
        1: {"display": TRIPLE_L_SHAPE[0], "source_name": "Capture primary"},
        2: {"display": TRIPLE_L_SHAPE[1], "source_name": "Capture right"},
        3: {"display": TRIPLE_L_SHAPE[2], "source_name": "Capture below"},
    }
    routing_script._settings["max_circles"] = 1
    routing_script._active_clicks[:] = [("__click_pop_L_0", 500)]

    routing_script._spawn_circle(1920, 1200, True, 999)

    routing_script._show_source.assert_not_called()
    routing_script._hide_source.assert_not_called()
    routing_script._get_capture_transform.assert_not_called()
    mock_obs.obs_frontend_get_current_scene.assert_not_called()
    assert routing_script._active_clicks == [("__click_pop_L_0", 500)]


def test_selected_secondary_accepts_its_last_pixel(routing_script):
    routing_script._all_displays = DUAL_SIDE_BY_SIDE
    routing_script._captured_display = DUAL_SIDE_BY_SIDE[1]
    routing_script._settings["capture_source"] = "Capture secondary"
    routing_script._get_capture_transform.return_value = {
        "capture_transform": (0.75, 0, 0, 0.75, 0, 0),
    }

    routing_script._spawn_circle(4479, 1439, True, 999)

    routing_script._show_source.assert_called_once()
    _, _, x, y, size = routing_script._show_source.call_args.args
    assert (x + size / 2, y + size / 2) == pytest.approx((1919.25, 1079.25))


@pytest.mark.parametrize("displays,captured_index,point", [
    (DUAL_SIDE_BY_SIDE, 1, (100, 100)),
    (DUAL_DPI_PHYSICAL, 0, (4000, 500)),
], ids=["secondary_selected", "physical_4k_primary_selected"])
def test_selected_capture_rejects_other_display(
        routing_script, displays, captured_index, point):
    routing_script._all_displays = displays
    routing_script._captured_display = displays[captured_index]
    routing_script._settings["capture_source"] = "Selected capture"

    routing_script._spawn_circle(*point, True, 999)

    routing_script._show_source.assert_not_called()
    routing_script._get_capture_transform.assert_not_called()
    assert routing_script._active_clicks == []
