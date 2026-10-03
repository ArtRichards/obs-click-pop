"""Linux display detection must use the same monitor IDs as OBS XSHM."""

import subprocess
from unittest.mock import Mock

import pytest


# Actual connector and active-monitor orders from the reported dual-screen setup.
CONNECTORS = """Screen 0: current 4480 x 1440
DisplayPort-2 connected 1920x1080+0+162 (normal left inverted right x axis y axis)
DisplayPort-1-0 connected primary 2560x1440+1920+0 (normal left inverted right x axis y axis)
"""
ACTIVE_MONITORS = """Monitors: 2
 0: +*DisplayPort-1-0 2560/697x1440/392+1920+0  DisplayPort-1-0
 1: +DisplayPort-2 1920/527x1080/296+0+162  DisplayPort-2
"""


@pytest.fixture
def xrandr(monkeypatch):
    command = Mock()
    monkeypatch.setattr(subprocess, "check_output", command)
    return command


@pytest.fixture
def linux_script(obs_script, monkeypatch):
    monkeypatch.setattr(obs_script.sys, "platform", "linux")
    obs_script.obs.obs_source_get_unversioned_id.return_value = "xshm_input_v2"
    obs_script.obs.obs_data_get_int.side_effect = lambda settings, key: settings.get(key, 0)
    return obs_script


def test_active_monitor_order_matches_obs_not_connector_order(linux_script, xrandr):
    xrandr.side_effect = lambda command, **kwargs: (
        ACTIVE_MONITORS if command == ["xrandr", "--listactivemonitors"] else CONNECTORS
    )

    displays = linux_script._detect_displays_linux()

    assert [(d["id"], d["x"], d["y"], d["w"], d["h"]) for d in displays] == [
        (0, 1920, 0, 2560, 1440),
        (1, 0, 162, 1920, 1080),
    ]
    assert [d["obs_screen"] for d in displays] == [0, 1]
    assert xrandr.call_args.args[0] == ["xrandr", "--listactivemonitors"]


def test_signed_origins_and_virtual_monitor_without_outputs(linux_script, xrandr):
    xrandr.return_value = """Monitors: 3
 0: +*Primary 2560/697x1440/392+0+0  DisplayPort-1
 1: Left 1920/0x1080/0-1920+162
 2: +Above 1920/527x1080/296+0-1080  HDMI-1
"""

    displays = linux_script._detect_displays_linux()

    assert [(d["x"], d["y"]) for d in displays] == [(0, 0), (-1920, 162), (0, -1080)]
    assert all(d["retina_scale"] == 1.0 for d in displays)


@pytest.mark.parametrize("error", [
    FileNotFoundError("xrandr is not installed"),
    subprocess.CalledProcessError(1, ["xrandr", "--listactivemonitors"]),
    subprocess.TimeoutExpired(["xrandr", "--listactivemonitors"], 5),
])
def test_detection_failure_does_not_guess_connector_indices(linux_script, xrandr, error):
    xrandr.side_effect = error

    assert linux_script._detect_displays_linux() == []
    xrandr.assert_called_once()
    linux_script.obs.script_log.assert_called()


@pytest.mark.parametrize("output", [
    "",
    CONNECTORS,
    "Monitors: 2\n 0: +DP-1 1920/500x1080/300+0+0 DP-1\n",
    "Monitors: 1\n 0: +DP-1 invalid-geometry DP-1\n",
    "Monitors: 1\n 0: +DP-1 0/500x1080/300+0+0 DP-1\n",
    "Monitors: 2\n 0: +DP-1 1920/500x1080/300+0+0 DP-1\n"
    " 0: +DP-2 1920/500x1080/300+1920+0 DP-2\n",
])
def test_malformed_monitor_list_is_not_used_for_matching(linux_script, xrandr, output):
    xrandr.return_value = output

    assert linux_script._detect_displays_linux() == []
    linux_script.obs.script_log.assert_called()


def test_no_active_monitors(linux_script, xrandr):
    xrandr.return_value = "Monitors: 0\n"

    assert linux_script._detect_displays_linux() == []


@pytest.mark.parametrize("screen,expected_origin", [(0, (1920, 0)), (1, (0, 162))])
def test_capture_source_resolves_actual_monitor_id(linux_script, xrandr, screen, expected_origin):
    xrandr.return_value = ACTIVE_MONITORS
    linux_script._all_displays = linux_script._detect_displays_linux()
    linux_script.obs.obs_source_get_settings.return_value = {"screen": screen}

    display = linux_script._resolve_display_for_source("Display Capture")

    assert (display["x"], display["y"]) == expected_origin


def test_capture_source_matches_id_not_display_list_position(linux_script, xrandr):
    xrandr.return_value = ACTIVE_MONITORS
    linux_script._all_displays = list(reversed(linux_script._detect_displays_linux()))
    linux_script.obs.obs_source_get_settings.return_value = {"screen": 0}

    display = linux_script._resolve_display_for_source("Display Capture")

    assert (display["x"], display["y"]) == (1920, 0)


def test_detection_fallback_is_used_only_as_the_single_known_display(linux_script, xrandr):
    # Without RandR 1.5 the synthetic display has no obs_screen to match,
    # but with exactly one display known there is nothing else to capture.
    xrandr.side_effect = FileNotFoundError("xrandr is not installed")
    linux_script._all_displays = linux_script._detect_all_displays()
    linux_script.obs.obs_source_get_settings.return_value = {"screen": 0}

    display = linux_script._resolve_display_for_source("Display Capture")

    assert display is linux_script._all_displays[0]
    assert "obs_screen" not in display


@pytest.mark.parametrize("screen", [0, 3])
def test_single_active_monitor_resolves_any_screen_id(linux_script, xrandr, screen):
    xrandr.return_value = "Monitors: 1\n 0: +*DP-1 2560/697x1440/392+0+0  DP-1\n"
    linux_script._all_displays = linux_script._detect_displays_linux()
    linux_script.obs.obs_source_get_settings.return_value = {"screen": screen}

    display = linux_script._resolve_display_for_source("Display Capture")

    assert (display["w"], display["h"]) == (2560, 1440)


def test_single_display_fallback_requires_a_display_capture(linux_script, xrandr):
    xrandr.return_value = "Monitors: 1\n 0: +*DP-1 2560/697x1440/392+0+0  DP-1\n"
    linux_script._all_displays = linux_script._detect_displays_linux()
    linux_script.obs.obs_source_get_unversioned_id.return_value = "xcomposite_input"
    linux_script.obs.obs_source_get_settings.return_value = {}

    assert linux_script._resolve_display_for_source("Window Capture") is None


def test_single_display_fallback_requires_an_existing_source(linux_script, xrandr):
    xrandr.return_value = "Monitors: 1\n 0: +*DP-1 2560/697x1440/392+0+0  DP-1\n"
    linux_script._all_displays = linux_script._detect_displays_linux()
    linux_script.obs.obs_get_source_by_name.return_value = None

    assert linux_script._resolve_display_for_source("Missing capture") is None


@pytest.mark.parametrize("screen", [-1, 99])
def test_unknown_capture_screen_is_not_guessed(linux_script, xrandr, screen):
    xrandr.return_value = ACTIVE_MONITORS
    linux_script._all_displays = linux_script._detect_displays_linux()
    linux_script.obs.obs_source_get_settings.return_value = {"screen": screen}

    assert linux_script._resolve_display_for_source("Display Capture") is None


def test_non_xshm_source_is_not_treated_as_screen_zero(linux_script, xrandr):
    xrandr.return_value = ACTIVE_MONITORS
    linux_script._all_displays = linux_script._detect_displays_linux()
    linux_script.obs.obs_source_get_unversioned_id.return_value = "xcomposite_input"
    linux_script.obs.obs_source_get_settings.return_value = {}

    assert linux_script._resolve_display_for_source("Window Capture") is None
