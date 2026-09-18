"""Capture pixels follow OBS draw transforms through cropped/nested groups."""

import json
from types import SimpleNamespace
from unittest.mock import call

import pytest


IDENTITY = (1, 0, 0, 1, 0, 0)


class Scene:
    def __init__(self, name):
        self.name = name
        self.items = []


class Item:
    def __init__(self, scene, name, affine=IDENTITY, *, group=None,
                 crop=(0, 0, 0, 0), filters=(0, 0, 0, 0), settings=None,
                 size=(200, 100), state=None):
        self.id = len(scene.items) + 1
        self.name = name
        self.affine = affine
        self.group = group
        self.crop = crop
        self.filters = filters
        self.settings = settings or {}
        self.size = size
        self.state = {
            "id": self.id, "scale": {"x": 1, "y": 1},
            "bounds_type": 0, "bounds": {"x": 100, "y": 100},
            "bounds_alignment": 0, "crop_to_bounds": False,
        }
        self.state.update(state or {})
        scene.items.append(self)


class Matrix4:
    def __init__(self):
        self.x = SimpleNamespace(x=0, y=0, z=0, w=0)
        self.y = SimpleNamespace(x=0, y=0, z=0, w=0)
        self.z = SimpleNamespace(x=0, y=0, z=1, w=0)
        self.t = SimpleNamespace(x=0, y=0, z=0, w=1)


@pytest.fixture
def graph(obs_script, mock_obs, monkeypatch):
    root = Scene("Root")
    saved = object()
    for index, suffix in enumerate((
        "NONE", "STRETCH", "SCALE_INNER", "SCALE_OUTER",
        "SCALE_TO_WIDTH", "SCALE_TO_HEIGHT", "MAX_ONLY",
    )):
        setattr(mock_obs, "OBS_BOUNDS_" + suffix, index)
    mock_obs.OBS_ALIGN_LEFT = 1
    mock_obs.OBS_ALIGN_RIGHT = 2
    mock_obs.OBS_ALIGN_TOP = 4
    mock_obs.OBS_ALIGN_BOTTOM = 8

    def snapshot(scene):
        groups = []

        def visit(current):
            for item in current.items:
                if item.group is not None:
                    visit(item.group)
            groups.append({"scene_name": current.name,
                           "is_group": current is not scene,
                           "items": [item.state for item in current.items]})

        visit(scene)
        return json.dumps({"scenes_and_groups": groups})

    mock_obs.obs_scene_save_transform_states.return_value = saved
    mock_obs.obs_data_get_json.side_effect = lambda data: snapshot(root)
    mock_obs.obs_scene_get_source.side_effect = lambda scene: scene
    mock_obs.obs_source_get_name.side_effect = lambda source: source.name
    mock_obs.obs_scene_find_sceneitem_by_id.side_effect = (
        lambda scene, item_id: next((item for item in scene.items if item.id == item_id), None)
    )
    mock_obs.obs_sceneitem_get_source.side_effect = lambda item: item
    mock_obs.obs_sceneitem_is_group.side_effect = lambda item: item.group is not None
    mock_obs.obs_sceneitem_group_get_scene.side_effect = lambda item: item.group
    mock_obs.obs_source_get_settings.side_effect = lambda source: source.settings
    mock_obs.obs_data_get_int.side_effect = lambda data, key: data.get(key, 0)
    mock_obs.obs_source_get_width.side_effect = lambda source: source.size[0]
    mock_obs.obs_source_get_height.side_effect = lambda source: source.size[1]
    mock_obs.matrix4 = Matrix4
    mock_obs.obs_sceneitem_crop.side_effect = lambda: SimpleNamespace(
        left=0, top=0, right=0, bottom=0,
    )

    def read_crop(item, crop):
        crop.left, crop.top, crop.right, crop.bottom = item.crop

    def read_matrix(item, matrix):
        xx, xy, yx, yy, tx, ty = item.affine
        matrix.x.x, matrix.y.x, matrix.x.y, matrix.y.y = xx, xy, yx, yy
        matrix.t.x, matrix.t.y = tx, ty

    mock_obs.obs_sceneitem_get_crop.side_effect = read_crop
    mock_obs.obs_sceneitem_get_draw_transform.side_effect = read_matrix
    monkeypatch.setattr(obs_script, "_get_filter_crop", lambda source: source.filters)
    # Model the old API accurately: its "recursive" lookup checks only one
    # group level, so nested captures require a genuine traversal.
    def shallow_find(scene, name):
        for item in scene.items:
            if item.name == name:
                return item
            if item.group is not None:
                for child in item.group.items:
                    if child.name == name:
                        return child
        return None

    mock_obs.obs_scene_find_source_recursive.side_effect = shallow_find
    mock_obs.obs_sceneitem_get_bounds_type.return_value = mock_obs.OBS_BOUNDS_NONE
    return root, saved


def _point(mapping, x, y):
    x -= mapping["crop_left"]
    y -= mapping["crop_top"]
    xx, xy, yx, yy, tx, ty = mapping["capture_transform"]
    return xx * x + xy * y + tx, yx * x + yy * y + ty


@pytest.mark.parametrize("affine", [
    (1, 0, 0, 1, 100, 50),
    IDENTITY,
    (0, -1, 1, 0, 100, 50),
    (-0.5, 0, 0, 0.5, 100, 25),
    (1.5, 0, 0, 0.75, 0, 112.5),
    (1, 0, 0, 1, 50, 100),
], ids=["position", "center_anchor", "rotation", "flipped_bounds",
        "nonuniform_bounds", "max_only_bounds"])
def test_capture_uses_native_draw_transform(obs_script, mock_obs, graph, affine):
    scene, saved = graph
    item = Item(scene, "Capture", affine)

    mapping = obs_script._get_capture_transform(scene, "Capture")

    assert mapping == {"crop_left": 0, "crop_top": 0, "capture_transform": affine}
    assert mock_obs.obs_sceneitem_get_draw_transform.call_args[0][0] is item
    mock_obs.obs_data_release.assert_any_call(saved)
    mock_obs.obs_source_release.assert_not_called()  # all source pointers borrowed


def test_source_and_item_crops_are_applied_once(obs_script, graph):
    scene, _ = graph
    Item(scene, "Capture", (0.5, 0, 0, 2, 200, 300),
         settings={"cut_left": 100, "cut_top": 50}, filters=(20, 10, 0, 0),
         crop=(30, 40, 0, 0))

    mapping = obs_script._get_capture_transform(scene, "Capture")

    assert mapping == {"crop_left": 120, "crop_top": 60,
                       "capture_transform": (0.5, 0, 0, 2, 185, 220)}
    assert _point(mapping, 250, 150) == (250, 400)


def test_transformed_group(obs_script, graph):
    scene, _ = graph
    group = Scene("Group")
    Item(scene, "Group", (0.5, 0, 0, 0.5, 100, 50), group=group)
    Item(group, "Capture")

    mapping = obs_script._get_capture_transform(scene, "Capture")

    assert _point(mapping, 480, 270) == (340, 185)


def test_nested_groups_compose_child_to_parent_with_local_ids(obs_script, graph):
    scene, _ = graph
    outer, inner = Scene("Outer"), Scene("Inner")
    Item(scene, "Outer", (0, -1, 1, 0, 500, 100), group=outer)
    Item(outer, "Inner", (2, 0, 0, 2, 20, 40), group=inner)
    Item(inner, "Capture", (0.5, 0, 0, 0.25, 10, 20))

    mapping = obs_script._get_capture_transform(scene, "Capture")

    assert mapping["capture_transform"] == (0, -0.5, 1, 0, 420, 140)
    assert _point(mapping, 100, 80) == (380, 240)


def test_group_filter_and_item_crops_follow_child_transform(obs_script, graph):
    scene, _ = graph
    group = Scene("Group")
    Item(scene, "Group", (0.5, 0, 0, 0.5, 100, 200), group=group,
         filters=(7, 11, 0, 0), crop=(5, 6, 0, 0))
    Item(group, "Capture", (1, 0, 0, 1, 40, 50), crop=(10, 20, 0, 0))

    mapping = obs_script._get_capture_transform(scene, "Capture")

    assert mapping["capture_transform"] == (0.5, 0, 0, 0.5, 109, 206.5)
    assert _point(mapping, 100, 80) == (159, 246.5)


@pytest.mark.parametrize("name", ["", "(none)", "(all - auto detect)"])
def test_no_selected_capture_returns_none(obs_script, mock_obs, graph, name):
    scene, _ = graph
    obs_script._settings["capture_source"] = name

    assert obs_script._get_capture_transform(scene) is None
    mock_obs.obs_scene_save_transform_states.assert_not_called()


def test_explicit_name_overrides_all_selection(obs_script, graph):
    scene, _ = graph
    Item(scene, "Capture")
    obs_script._settings["capture_source"] = obs_script._ALL_CAPTURES_LABEL

    assert obs_script._get_capture_transform(scene, "Capture")["capture_transform"] == IDENTITY


def test_missing_source_returns_none(obs_script, mock_obs, graph):
    scene, saved = graph
    Item(scene, "Other capture")

    assert obs_script._get_capture_transform(scene, "Missing") is None
    mock_obs.obs_data_release.assert_called_once_with(saved)
    mock_obs.obs_source_get_settings.assert_not_called()


def test_missing_scene_returns_none(obs_script, mock_obs, graph):
    assert obs_script._get_capture_transform(None, "Capture") is None
    mock_obs.obs_scene_save_transform_states.assert_not_called()


def test_snapshot_is_released_when_read_fails(obs_script, mock_obs, graph):
    scene, saved = graph
    mock_obs.obs_data_get_json.side_effect = RuntimeError("snapshot read failed")

    with pytest.raises(RuntimeError, match="snapshot read failed"):
        obs_script._get_capture_transform(scene, "Capture")

    mock_obs.obs_data_release.assert_called_once_with(saved)


def test_source_settings_are_released_when_read_fails(obs_script, mock_obs, graph):
    scene, saved = graph
    item = Item(scene, "Capture")
    mock_obs.obs_data_get_int.side_effect = RuntimeError("settings read failed")

    with pytest.raises(RuntimeError, match="settings read failed"):
        obs_script._get_capture_transform(scene, "Capture")

    assert mock_obs.obs_data_release.call_args_list == [call(saved), call(item.settings)]


@pytest.mark.parametrize("size, scale, alignment, native, expected", [
    ((200, 100), (1, 1), 0, IDENTITY, (1, 0, 0, 1, -50, 0)),
    ((200, 100), (1, 1), 1, IDENTITY, IDENTITY),
    ((200, 100), (1, 1), 2, IDENTITY, (1, 0, 0, 1, -100, 0)),
    ((200, 100), (-1, 1), 0, (-1, 0, 0, 1, 100, 0), (-1, 0, 0, 1, 150, 0)),
    ((200, 100), (-1, 1), 1, (-1, 0, 0, 1, 100, 0), (-1, 0, 0, 1, 200, 0)),
    ((200, 100), (-1, 1), 2, (-1, 0, 0, 1, 100, 0), (-1, 0, 0, 1, 100, 0)),
    ((201, 100), (1, 1), 0, IDENTITY, (1, 0, 0, 1, -51, 0)),
    ((100, 200), (1, 1), 0, IDENTITY, (1, 0, 0, 1, 0, -50)),
], ids=["center", "left", "right", "flipped_center", "flipped_left",
        "flipped_right", "round_half_up", "vertical"])
def test_crop_to_bounds_includes_hidden_texture_crop(
        obs_script, mock_obs, graph, size, scale, alignment, native, expected):
    scene, _ = graph
    Item(scene, "Capture", native, size=size, state={
        "bounds_type": mock_obs.OBS_BOUNDS_SCALE_OUTER,
        "crop_to_bounds": True, "bounds_alignment": alignment,
        "scale": {"x": scale[0], "y": scale[1]},
    })

    assert obs_script._get_capture_transform(scene, "Capture")["capture_transform"] == expected


def test_disabled_bounds_crop_keeps_native_overflow_offset(obs_script, graph):
    scene, _ = graph
    native = (1, 0, 0, 1, -50, 0)
    Item(scene, "Capture", native, state={"crop_to_bounds": False})

    assert obs_script._get_capture_transform(scene, "Capture")["capture_transform"] == native


@pytest.mark.parametrize("kind, size, expected", [
    ("SCALE_TO_WIDTH", (100, 200), (1, 0, 0, 1, 0, -50)),
    ("SCALE_TO_HEIGHT", (200, 100), (1, 0, 0, 1, -50, 0)),
])
def test_width_and_height_bounds_crop(obs_script, mock_obs, graph, kind, size, expected):
    scene, _ = graph
    Item(scene, "Capture", size=size, state={
        "bounds_type": getattr(mock_obs, "OBS_BOUNDS_" + kind),
        "crop_to_bounds": True,
    })

    assert obs_script._get_capture_transform(scene, "Capture")["capture_transform"] == expected


def test_bounds_crop_uses_size_after_manual_crop(obs_script, mock_obs, graph):
    scene, _ = graph
    Item(scene, "Capture", crop=(20, 0, 20, 0), state={
        "bounds_type": mock_obs.OBS_BOUNDS_SCALE_OUTER,
        "crop_to_bounds": True,
    })

    # 200-wide source -> 160 after manual crop -> 100 after bounds crop:
    # left offset = manual20 + centered bounds30, not manual20+bounds50.
    mapping = obs_script._get_capture_transform(scene, "Capture")
    assert mapping["capture_transform"] == (1, 0, 0, 1, -50, 0)


def test_nonuniform_bounds_crop_preserves_relative_scale(obs_script, mock_obs, graph):
    scene, _ = graph
    Item(scene, "Capture", (2, 0, 0, 1, 0, 0), size=(200, 100), state={
        "scale": {"x": 2, "y": 1},
        "bounds_type": mock_obs.OBS_BOUNDS_SCALE_OUTER,
        "crop_to_bounds": True,
    })

    # Scaled width400 overflows bounds100 by300 ->150 source pixels total,
    # 75 cropped from the left, giving canvas translation -2*75=-150.
    mapping = obs_script._get_capture_transform(scene, "Capture")
    assert mapping["capture_transform"] == (2, 0, 0, 1, -150, 0)


@pytest.mark.parametrize("automatic", [False, True], ids=["explicit", "all_single_monitor"])
def test_spawn_circle_uses_real_grouped_capture_mapping(
        obs_script, mock_obs, monkeypatch, graph, automatic):
    scene, _ = graph
    scene.size = (1920, 1080)
    group = Scene("Group")
    Item(scene, "Group", (0.5, 0, 0, 0.5, 100, 50), group=group)
    Item(group, "Capture", settings={"cut_left": 100, "cut_top": 50},
         filters=(20, 10, 0, 0), crop=(10, 20, 0, 0))
    display = {"id": 7, "x": 1920, "y": 0, "w": 1920, "h": 1080,
               "retina_scale": 1.0}
    obs_script._all_displays = [display]
    obs_script._captured_display = None if automatic else display
    obs_script._multi_capture_mode = automatic
    obs_script._display_capture_map = {
        7: {"display": display, "source_name": "Capture"},
    }
    obs_script._settings.update({
        "capture_source": obs_script._ALL_CAPTURES_LABEL if automatic else "Capture",
        "circle_size": 60,
    })
    mock_obs.obs_frontend_get_current_scene.return_value = scene
    mock_obs.obs_scene_from_source.return_value = scene
    shown = []
    monkeypatch.setattr(obs_script, "_show_source", lambda *args: shown.append(args))

    obs_script._spawn_circle(2400, 270, True, 1000)

    # Desktop -> local(480,270) -> source(360,210) -> item(350,190)
    # -> grouped canvas center(275,145) -> 60px marker top-left(245,115).
    assert len(shown) == 1
    assert shown[0][2:] == (245, 115, 60)
    mock_obs.obs_source_release.assert_called_once_with(scene)
