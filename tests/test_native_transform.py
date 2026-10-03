"""Native tier: the script's transform math against headless libobs.

These tests load the real ``obspython`` bindings, start libobs without
video or a frontend, build scenes with genuine scene items, groups, crops
and bounds, and check that the script maps capture pixels exactly where
libobs' own draw transforms and ``vec3_transform`` put them.

Run with ``pytest -m native``.  Set ``OBS_SCRIPTING_DIR`` to the directory
containing ``obspython.py`` and ``_obspython.so`` if it is not the Linux
default.  Sources are unregistered placeholders (no plugins are loaded), so
they report a 0x0 size; a 1px manual crop makes libobs treat them as 2x2,
which is enough to exercise every bounds mode.
"""

import importlib
import json
import os
import sys

import pytest

pytestmark = pytest.mark.native

_DEFAULT_DIRS = ("/usr/lib/x86_64-linux-gnu/obs-scripting", "/usr/lib/obs-scripting")


def _load_native_obs():
    for directory in (os.environ.get("OBS_SCRIPTING_DIR"), *_DEFAULT_DIRS):
        if directory and os.path.exists(os.path.join(directory, "obspython.py")):
            if directory not in sys.path:
                sys.path.insert(0, directory)
            break
    try:
        import obspython
    except ImportError as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"native obspython bindings unavailable: {exc}")
    if not hasattr(obspython, "obs_startup"):
        pytest.skip("obspython in sys.modules is not the native module")
    return obspython


@pytest.fixture(scope="session")
def obs():
    native = _load_native_obs()
    sys.modules["obspython"] = native
    if not native.obs_startup("en-US", None, None):
        pytest.skip("libobs failed to start headless")
    yield native
    native.obs_shutdown()


@pytest.fixture(scope="session")
def script(obs):
    sys.modules.pop("obs_click_pop", None)
    module = importlib.import_module("obs_click_pop")
    yield module
    sys.modules.pop("obs_click_pop", None)


class Builder:
    """Create native scenes/items and release every reference afterwards."""

    def __init__(self, obs):
        self.obs = obs
        self.sources = []
        self.scene = obs.obs_scene_create("Root")

    def source(self, name, kind="xshm_input_v2", **settings):
        data = self.obs.obs_data_create()
        for key, value in settings.items():
            self.obs.obs_data_set_int(data, key, value)
        source = self.obs.obs_source_create(kind, name, data, None)
        self.obs.obs_data_release(data)
        self.sources.append(source)
        return source

    def item(self, name, scene=None, *, pos=(0, 0), scale=(1, 1), rot=0,
             crop=(0, 0, 0, 0), bounds=None, bounds_align=0, align=5,
             settings=None, filters=()):
        source = self.source(name, **(settings or {}))
        for left, top in filters:
            data = self.obs.obs_data_create()
            self.obs.obs_data_set_int(data, "left", left)
            self.obs.obs_data_set_int(data, "top", top)
            crop_filter = self.obs.obs_source_create(
                "crop_filter", f"{name} crop {left},{top}", data, None)
            self.obs.obs_data_release(data)
            self.obs.obs_source_filter_add(source, crop_filter)
            self.sources.append(crop_filter)
        item = self.obs.obs_scene_add(scene or self.scene, source)
        self.transform(item, pos=pos, scale=scale, rot=rot, crop=crop,
                       bounds=bounds, bounds_align=bounds_align, align=align)
        return item

    def group(self, name, children, **transform):
        group = self.obs.obs_scene_add_group(self.scene, name)
        for child in children:
            self.obs.obs_sceneitem_group_add_item(group, child)
        self.transform(group, **transform)
        return group

    def transform(self, item, *, pos=(0, 0), scale=(1, 1), rot=0,
                  crop=(0, 0, 0, 0), bounds=None, bounds_align=0, align=5):
        obs = self.obs
        vec = obs.vec2()
        vec.x, vec.y = pos
        obs.obs_sceneitem_set_pos(item, vec)
        vec = obs.vec2()
        vec.x, vec.y = scale
        obs.obs_sceneitem_set_scale(item, vec)
        obs.obs_sceneitem_set_rot(item, rot)
        obs.obs_sceneitem_set_alignment(item, align)
        item_crop = obs.obs_sceneitem_crop()
        item_crop.left, item_crop.top, item_crop.right, item_crop.bottom = crop
        obs.obs_sceneitem_set_crop(item, item_crop)
        if bounds is not None:
            kind, width, height = bounds
            vec = obs.vec2()
            vec.x, vec.y = width, height
            obs.obs_sceneitem_set_bounds(item, vec)
            obs.obs_sceneitem_set_bounds_alignment(item, bounds_align)
            obs.obs_sceneitem_set_bounds_type(item, getattr(obs, "OBS_BOUNDS_" + kind))

    def close(self):
        for source in reversed(self.sources):
            self.obs.obs_source_release(source)
        self.obs.obs_scene_release(self.scene)


@pytest.fixture
def build(obs):
    builder = Builder(obs)
    yield builder
    builder.close()


def native_point(obs, path, x, y):
    """Map a source pixel through libobs exactly as the renderer does.

    Each level crops by texture offset, then multiplies by that item's own
    draw matrix; groups wrap their children the same way.
    """
    point = obs.vec3()
    obs.vec3_set(point, x, y, 0)
    for item in reversed(path):
        crop = obs.obs_sceneitem_crop()
        obs.obs_sceneitem_get_crop(item, crop)
        shifted = obs.vec3()
        obs.vec3_set(shifted, point.x - crop.left, point.y - crop.top, 0)
        matrix = obs.matrix4()
        obs.obs_sceneitem_get_draw_transform(item, matrix)
        point = obs.vec3()
        obs.vec3_transform(point, shifted, matrix)
    return point.x, point.y


def script_point(mapping, x, y):
    x -= mapping["crop_left"]
    y -= mapping["crop_top"]
    xx, xy, yx, yy, tx, ty = mapping["capture_transform"]
    return xx * x + xy * y + tx, yx * x + yy * y + ty


POINTS = [(0, 0), (10, 20), (333, 7), (1920, 1080)]


@pytest.mark.parametrize("transform", [
    dict(),
    dict(pos=(100, 50)),
    dict(pos=(100, 50), scale=(2, 0.5)),
    dict(pos=(100, 50), scale=(2, 0.5), rot=90),
    dict(pos=(300, 200), rot=37.5),
    dict(pos=(100, 50), scale=(-1, 1)),
    dict(pos=(100, 50), scale=(1, -1), rot=180),
    dict(pos=(100, 50), align=0),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("STRETCH", 100, 50)),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("SCALE_INNER", 100, 50)),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("SCALE_OUTER", 100, 50)),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("SCALE_TO_WIDTH", 100, 50)),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("SCALE_TO_HEIGHT", 100, 50)),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("MAX_ONLY", 100, 50)),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("SCALE_INNER", 100, 50),
         bounds_align=1 | 4),
    dict(pos=(100, 50), crop=(1, 1, 0, 0), bounds=("SCALE_INNER", 100, 50),
         bounds_align=2 | 8, scale=(-1, -1), rot=45),
], ids=["identity", "position", "scale", "rotation", "odd_rotation", "flip_x",
        "flip_y_rot180", "center_anchor", "stretch", "inner", "outer",
        "to_width", "to_height", "max_only", "inner_top_left",
        "inner_bottom_right_flipped_rotated"])
def test_single_item_matches_native_draw_transform(obs, script, build, transform):
    item = build.item("Capture", **transform)

    mapping = script._get_capture_transform(build.scene, "Capture")

    for x, y in POINTS:
        assert script_point(mapping, x, y) == pytest.approx(
            native_point(obs, [item], x, y), rel=1e-6, abs=1e-3)


def test_matrix4_unpack_matches_vec3_transform_rows(obs, script, build):
    """The SWIG matrix4 is row-vector: x.y is the x->y coefficient."""
    item = build.item("Capture", pos=(100, 50), scale=(2, 0.5), rot=90)

    xx, xy, yx, yy, tx, ty = script._get_capture_transform(
        build.scene, "Capture")["capture_transform"]

    assert (xx, xy, yx, yy, tx, ty) == pytest.approx((0, -0.5, 2, 0, 100, 50), abs=1e-6)


def test_manual_item_crop_is_subtracted_once(obs, script, build):
    item = build.item("Capture", pos=(100, 50), scale=(2, 2), crop=(30, 40, 5, 6))

    mapping = script._get_capture_transform(build.scene, "Capture")

    assert (mapping["crop_left"], mapping["crop_top"]) == (0, 0)
    assert script_point(mapping, 130, 140) == pytest.approx(
        native_point(obs, [item], 130, 140))
    assert script_point(mapping, 130, 140) == pytest.approx((300, 250))


def test_source_settings_and_filter_crops_precede_the_item(obs, script, build):
    item = build.item("Capture", pos=(100, 50), scale=(2, 2), crop=(30, 40, 0, 0),
                      settings={"cut_left": 100, "cut_top": 50},
                      filters=[(20, 10), (5, 5)])

    mapping = script._get_capture_transform(build.scene, "Capture")

    assert (mapping["crop_left"], mapping["crop_top"]) == (125, 65)
    # libobs sees the item only after the source/filter crops shifted it.
    assert script_point(mapping, 125 + 30 + 7, 65 + 40 + 9) == pytest.approx(
        native_point(obs, [item], 30 + 7, 40 + 9))


def test_disabled_filter_crop_is_ignored(obs, script, build):
    build.item("Capture", filters=[(20, 10)])
    disabled = build.sources[-1]
    obs.obs_source_set_enabled(disabled, False)

    mapping = script._get_capture_transform(build.scene, "Capture")

    assert (mapping["crop_left"], mapping["crop_top"]) == (0, 0)


@pytest.mark.parametrize("group_transform", [
    dict(pos=(300, 200), scale=(0.5, 0.5)),
    dict(pos=(300, 200), scale=(0.5, 0.5), rot=90),
    dict(pos=(300, 200), scale=(-0.5, 0.5), rot=30),
], ids=["scaled", "rotated", "flipped_rotated"])
def test_grouped_capture_composes_child_then_group(obs, script, build, group_transform):
    child = build.item("Capture", pos=(100, 50), scale=(2, 0.5), rot=90,
                       crop=(10, 20, 0, 0))
    build.item("Other", pos=(1, 1))
    group = build.group("Group", [child], **group_transform)

    mapping = script._get_capture_transform(build.scene, "Capture")

    for x, y in POINTS:
        assert script_point(mapping, x, y) == pytest.approx(
            native_point(obs, [group, child], x, y), rel=1e-6, abs=1e-3)


def test_group_item_ids_are_resolved_in_the_group_not_the_root(obs, script, build):
    # Root item 1 and the group's item 1 share an ID; only the group's is
    # the capture.  A root-level lookup would pick the decoy.
    decoy = build.item("Decoy", pos=(999, 999))
    child = build.item("Capture", pos=(100, 50))
    group = build.group("Group", [child], pos=(300, 200))
    snapshot = obs.obs_scene_save_transform_states(build.scene, True)
    try:
        states = json.loads(obs.obs_data_get_json(snapshot))
    finally:
        obs.obs_data_release(snapshot)
    by_name = {entry["scene_name"]: [i["id"] for i in entry["items"]]
               for entry in states["scenes_and_groups"]}
    assert obs.obs_sceneitem_get_id(decoy) in by_name["Root"]
    assert obs.obs_sceneitem_get_id(child) in by_name["Group"]

    path = script._find_capture_path(build.scene, "Capture")

    assert [obs.obs_sceneitem_get_id(item) for item, _ in path] == [
        obs.obs_sceneitem_get_id(group), obs.obs_sceneitem_get_id(child)]
    # libobs re-bases a child's position when it joins a group, so compare
    # against the native result rather than a hand-computed canvas point.
    mapping = script._get_capture_transform(build.scene, "Capture")
    assert script_point(mapping, 0, 0) == pytest.approx(
        native_point(obs, [group, child], 0, 0))
    assert script_point(mapping, 0, 0) != pytest.approx(
        native_point(obs, [decoy], 0, 0))


def test_missing_capture_returns_none_without_leaking(obs, script, build):
    build.item("Other")

    assert script._get_capture_transform(build.scene, "Capture") is None


def test_crop_to_bounds_hidden_offset_matches_native(obs, script, build):
    if not hasattr(obs, "obs_sceneitem_set_bounds_crop"):
        pytest.skip("Crop to Bounding Box needs libobs 30.1+")
    item = build.item("Capture", pos=(100, 50), crop=(1, 1, 0, 0),
                      bounds=("SCALE_OUTER", 100, 50))
    obs.obs_sceneitem_set_bounds_crop(item, True)

    mapping = script._get_capture_transform(build.scene, "Capture")

    # The 2x2 source scales 50x to 100x100 inside 100x50 bounds, so libobs
    # crops 1 source pixel (0.5 rounded up) from the top before drawing.
    native_origin = native_point(obs, [item], 0, 0)
    assert script_point(mapping, 0, 1) == pytest.approx(native_origin)
