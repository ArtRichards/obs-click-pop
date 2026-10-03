"""Crop/Pad offsets follow the enabled OBS filter chain."""

import json
from unittest.mock import call

import pytest


def _set_filters(mock_obs, entries):
    """Expose saved filter records through the OBS backup-data API."""
    filters = object()
    records = [object() for _ in entries]
    serialized = {
        record: json.dumps(entry) if entry is not None else ""
        for record, entry in zip(records, entries)
    }
    mock_obs.obs_source_backup_filters.return_value = filters
    mock_obs.obs_data_array_count.return_value = len(records)
    mock_obs.obs_data_array_item.side_effect = lambda array, index: records[index]
    mock_obs.obs_data_get_json.side_effect = serialized.__getitem__
    return filters, records


@pytest.mark.parametrize(
    "entries, expected",
    [
        ([], (0, 0)),
        ([None], (0, 0)),
        ([{"id": "crop_filter"}], (0, 0)),
        ([{
            "id": "crop_filter",
            "settings": {"left": 100, "top": 50, "right": 20, "bottom": 10},
        }], (100, 50)),
        ([{
            "id": "crop_filter", "enabled": False,
            "settings": {"left": 100, "top": 50, "right": 20, "bottom": 10},
        }], (0, 0)),
        ([{
            "id": "crop_filter", "enabled": False,
            "settings": {"left": 100, "top": 50},
        }, {
            "id": "crop_filter", "enabled": True,
            "settings": {"left": 20, "top": 10},
        }], (20, 10)),
        ([{
            "id": "crop_filter", "enabled": True,
            "settings": {"left": 100, "top": 50, "right": 20, "bottom": 10},
        }, {
            "id": "crop_filter", "enabled": True,
            "settings": {"left": 20, "top": 10, "right": 4, "bottom": 2},
        }], (120, 60)),
        ([{
            "id": "color_filter", "enabled": True,
            "settings": {"left": 999, "top": 999},
        }, {
            "id": "crop_filter", "enabled": True,
            "settings": {"left": 100, "top": 50},
        }, {
            "id": "crop_filter", "enabled": False,
            "settings": {"left": 999, "top": 999},
        }, {
            "id": "crop_filter",
            "settings": {"left": 20, "top": 10},
        }], (120, 60)),
        ([{
            "id": "crop_filter",
            "settings": {"left": 100, "top": 50, "right": 20, "bottom": 10},
        }, {
            "id": "crop_filter",
            "settings": {"left": -20, "top": -10, "right": -4, "bottom": -2},
        }], (80, 40)),
    ],
    ids=[
        "no_filters", "empty_record", "default_settings", "default_enabled",
        "disabled", "disabled_before_enabled", "stacked_enabled",
        "mixed_filter_chain", "negative_padding",
    ],
)
def test_filter_crop_uses_all_enabled_crops(obs_script, mock_obs, entries, expected):
    filters, records = _set_filters(mock_obs, entries)

    assert obs_script._get_filter_crop(object()) == expected

    assert mock_obs.obs_data_release.call_args_list == [call(record) for record in records]
    mock_obs.obs_data_array_release.assert_called_once_with(filters)


def test_absolute_crop_keeps_origin_offsets(obs_script, mock_obs):
    _set_filters(mock_obs, [{
        "id": "crop_filter", "enabled": True,
        "settings": {
            "relative": False, "left": 100, "top": 50,
            "cx": 640, "cy": 480, "right": 999, "bottom": 999,
        },
    }, {
        "id": "crop_filter", "enabled": True,
        "settings": {"left": 20, "top": 10, "right": 4, "bottom": 2},
    }])

    # Absolute mode still shifts the origin; output size comes from OBS.
    assert obs_script._get_filter_crop(object()) == (120, 60)


@pytest.mark.parametrize("failure", ["read_json", "parse_json", "count_records"])
def test_filter_crop_releases_refs_on_errors(obs_script, mock_obs, failure):
    filters, records = _set_filters(mock_obs, [{"id": "crop_filter"}])
    if failure == "read_json":
        mock_obs.obs_data_get_json.side_effect = RuntimeError("cannot read filter")
        expected_error = RuntimeError
    elif failure == "parse_json":
        mock_obs.obs_data_get_json.side_effect = None
        mock_obs.obs_data_get_json.return_value = "not JSON"
        expected_error = json.JSONDecodeError
    else:
        mock_obs.obs_data_array_count.side_effect = RuntimeError("cannot read filter count")
        expected_error = RuntimeError

    with pytest.raises(expected_error):
        obs_script._get_filter_crop(object())

    if failure != "count_records":
        mock_obs.obs_data_release.assert_called_once_with(records[0])
    else:
        mock_obs.obs_data_release.assert_not_called()
    mock_obs.obs_data_array_release.assert_called_once_with(filters)
