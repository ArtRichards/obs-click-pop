"""Capture discovery must resolve item IDs in their own scenes/groups."""

import json
from unittest.mock import call

import pytest


@pytest.fixture()
def capture_tree(mock_obs):
    group_source, group_scene = object(), object()
    root_capture, grouped_capture = object(), object()
    root_item, group_item, child_item = object(), object(), object()
    names = {root_capture: "Root capture", grouped_capture: "Grouped capture",
             group_source: "Group"}
    item_sources = {root_item: root_capture, group_item: group_source,
                    child_item: grouped_capture}
    items = {(mock_obs._scene, 1): root_item, (mock_obs._scene, 2): group_item,
             (group_scene, 1): child_item}
    snapshot = {"scenes_and_groups": [
        {"scene_name": "Scene", "is_group": False,
         "items": [{"id": 1}, {"id": 2}]},
        {"scene_name": "Group", "is_group": True, "items": [{"id": 1}]},
    ]}
    mock_obs.obs_data_get_json.side_effect = lambda data: json.dumps(snapshot)
    mock_obs.obs_get_source_by_name.side_effect = lambda name: (
        group_source if name == "Group" else None)
    mock_obs.obs_group_from_source.return_value = group_scene
    mock_obs.obs_scene_find_sceneitem_by_id.side_effect = lambda scene, item_id: (
        items.get((scene, item_id)))
    mock_obs.obs_sceneitem_get_source.side_effect = item_sources.__getitem__
    mock_obs.obs_source_get_unversioned_id.side_effect = lambda source: (
        "group" if source is group_source else "xshm_input")
    mock_obs.obs_source_get_name.side_effect = names.__getitem__
    return group_source, group_scene, snapshot, names


def test_discovery_resolves_group_local_ids(obs_script, mock_obs, capture_tree):
    group_source, group_scene, _, _ = capture_tree

    assert list(obs_script._iter_display_capture_names()) == [
        "Root capture", "Grouped capture",
    ]
    assert call(group_scene, 1) in mock_obs.obs_scene_find_sceneitem_by_id.call_args_list
    assert call(group_source) in mock_obs.obs_source_release.call_args_list
    assert call(mock_obs._scene_source) in mock_obs.obs_source_release.call_args_list
    mock_obs.obs_data_release.assert_called_once_with(
        mock_obs.obs_scene_save_transform_states.return_value)


@pytest.mark.parametrize("missing", ["source", "scene"])
def test_missing_group_does_not_alias_root_item_ids(
        obs_script, mock_obs, capture_tree, missing):
    if missing == "source":
        mock_obs.obs_get_source_by_name.side_effect = lambda name: None
    else:
        mock_obs.obs_group_from_source.return_value = None

    assert list(obs_script._iter_display_capture_names()) == ["Root capture"]


def test_reused_capture_name_is_listed_once(obs_script, mock_obs, capture_tree):
    _, _, _, names = capture_tree
    for source, name in list(names.items()):
        if name != "Group":
            names[source] = "Shared capture"

    assert list(obs_script._iter_display_capture_names()) == ["Shared capture"]


def test_no_scene_returns_no_captures(obs_script, mock_obs):
    mock_obs.obs_frontend_get_current_scene.return_value = None

    assert list(obs_script._iter_display_capture_names()) == []
    mock_obs.obs_scene_from_source.assert_not_called()


def test_discovery_error_releases_group_and_scene_refs(
        obs_script, mock_obs, capture_tree):
    group_source, group_scene, _, _ = capture_tree
    find_item = mock_obs.obs_scene_find_sceneitem_by_id.side_effect

    def fail_on_child(scene, item_id):
        if scene is group_scene:
            raise RuntimeError("cannot read group")
        return find_item(scene, item_id)

    mock_obs.obs_scene_find_sceneitem_by_id.side_effect = fail_on_child

    with pytest.raises(RuntimeError, match="cannot read group"):
        list(obs_script._iter_display_capture_names())
    assert call(group_source) in mock_obs.obs_source_release.call_args_list
    assert call(mock_obs._scene_source) in mock_obs.obs_source_release.call_args_list
