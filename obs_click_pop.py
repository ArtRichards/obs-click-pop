import obspython as obs
import sys
import time
import os
from collections import deque

from click_pop_core import map_coords, allocate_slot, expire_circles, find_display_for_point


# ---------------------------------------------------------------------------
# Display detection
# ---------------------------------------------------------------------------

def _detect_all_displays():
    """Return a list of display descriptors for all connected monitors.

    Each descriptor is a dict with keys:
      id            – platform display identifier
      x, y          – origin in virtual desktop space (logical points)
      w, h          – logical resolution
      retina_scale  – backing scale factor (2.0 on macOS Retina, else 1.0)

    Falls back to a single 1920x1080 display if detection fails.
    """
    import subprocess
    displays = []
    try:
        if sys.platform == "win32":
            displays = _detect_displays_win32()
        elif sys.platform == "darwin":
            displays = _detect_displays_macos()
        else:
            displays = _detect_displays_linux()
    except Exception:
        pass
    if not displays:
        displays = [{"id": 0, "x": 0, "y": 0, "w": 1920, "h": 1080,
                      "retina_scale": 1.0}]
    return displays


def _detect_displays_macos():
    """Enumerate displays on macOS via Quartz."""
    import Quartz
    max_displays = 16
    (err, display_ids, count) = Quartz.CGGetActiveDisplayList(max_displays, None, None)
    if err != 0:
        return []
    displays = []
    for did in display_ids[:count]:
        bounds = Quartz.CGDisplayBounds(did)
        w, h = int(bounds.size.width), int(bounds.size.height)
        x, y = int(bounds.origin.x), int(bounds.origin.y)
        retina_scale = 1.0
        try:
            mode = Quartz.CGDisplayCopyDisplayMode(did)
            if mode is not None:
                pw = Quartz.CGDisplayModeGetPixelWidth(mode)
                if pw and w > 0:
                    retina_scale = pw / w
        except Exception:
            pass
        displays.append({"id": did, "x": x, "y": y, "w": w, "h": h,
                          "retina_scale": retina_scale})
    return displays


def _detect_displays_win32():
    """Enumerate displays on Windows via ctypes.

    Returns display rects in physical-pixel coordinates so they match the
    coordinate space that pynput's WH_MOUSE_LL hook reports.

    Each display dict includes:
      - ``device_name``: GDI name like ``\\\\.\\DISPLAY1``
      - ``device_path``: PnP device interface path used by OBS 28+ as
        ``monitor_id`` (e.g. ``\\\\?\\DISPLAY#...#{guid}``)

    To get physical-pixel rects we temporarily set the calling thread's DPI
    awareness to Per-Monitor Aware V2 before calling EnumDisplayMonitors.
    On older Windows (pre-1607) where SetThreadDpiAwarenessContext is
    unavailable the call is skipped — at 100 % scaling the coordinates
    already match.
    """
    import ctypes
    import ctypes.wintypes

    # MONITORINFOEXW — extends MONITORINFO with a 32-wchar device name.
    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork", ctypes.wintypes.RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    # DISPLAY_DEVICEW — used by EnumDisplayDevicesW to get PnP device path.
    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("DeviceName", ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags", ctypes.wintypes.DWORD),
            ("DeviceID", ctypes.c_wchar * 128),
            ("DeviceKey", ctypes.c_wchar * 128),
        ]

    displays = []
    user32 = ctypes.windll.user32

    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL

    user32.EnumDisplayDevicesW.argtypes = [
        ctypes.c_wchar_p, ctypes.wintypes.DWORD,
        ctypes.POINTER(DISPLAY_DEVICEW), ctypes.wintypes.DWORD,
    ]
    user32.EnumDisplayDevicesW.restype = ctypes.wintypes.BOOL

    # --- Temporarily switch to per-monitor DPI awareness (v2) -----------
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    old_ctx = None
    try:
        _SetThreadDpiAwarenessContext = user32.SetThreadDpiAwarenessContext
        _SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        old_ctx = _SetThreadDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except (OSError, AttributeError):
        # Pre-Windows 10 1607 — function doesn't exist; proceed without it.
        pass

    try:
        # HMONITOR and HDC are pointer-sized handles (8 bytes on 64-bit).
        # LPARAM is also pointer-sized.
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,   # hMonitor
            ctypes.c_void_p,   # hdcMonitor
            ctypes.POINTER(ctypes.wintypes.RECT),  # lprcMonitor
            ctypes.wintypes.LPARAM,                 # dwData
        )

        def callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
            r = lprcMonitor[0]

            # Get GDI device name (e.g. \\.\DISPLAY1) via GetMonitorInfoW.
            device_name = ""
            try:
                info = MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
                    device_name = info.szDevice
            except Exception:
                pass

            displays.append({
                "id": hMonitor,
                "x": r.left, "y": r.top,
                "w": r.right - r.left, "h": r.bottom - r.top,
                "retina_scale": 1.0,
                "device_name": device_name,
                "device_path": "",
            })
            return 1  # continue enumeration

        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    finally:
        # --- Restore previous DPI awareness context ---------------------
        if old_ctx is not None:
            try:
                user32.SetThreadDpiAwarenessContext(old_ctx)
            except (OSError, AttributeError):
                pass

    # --- Resolve PnP device interface paths for each display ------------
    # OBS 28+ stores monitor_id as a device interface path (e.g.
    # \\?\DISPLAY#HW_ID#INSTANCE#{GUID}).  EnumDisplayDevicesW with
    # EDD_GET_DEVICE_INTERFACE_NAME gives us this path for each GDI name.
    EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001
    for d in displays:
        dev_name = d.get("device_name", "")
        if not dev_name:
            continue
        try:
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if user32.EnumDisplayDevicesW(
                    dev_name, 0, ctypes.byref(dd),
                    EDD_GET_DEVICE_INTERFACE_NAME):
                d["device_path"] = dd.DeviceID
        except Exception:
            pass

    return displays


def _detect_displays_linux():
    """Enumerate active RandR monitors using OBS XSHM's screen numbering.

    RandR 1.5's active-monitor list can differ from the connector order in
    ``xrandr --query``.  Preserve the explicit monitor number as ``obs_screen``
    so a fallback display descriptor cannot accidentally match screen zero.
    Older xrandr/RandR versions without this list leave automatic matching
    unavailable; guessing connector indices would select the wrong display.
    """
    import subprocess, re
    try:
        out = subprocess.check_output(
            ["xrandr", "--listactivemonitors"], text=True, timeout=5,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        obs.script_log(obs.LOG_WARNING,
                       f"Click Pop: active RandR monitor detection failed: {exc}; "
                       "automatic display matching is unavailable")
        return []

    header = re.search(r"^Monitors:\s*(\d+)\s*$", out, re.MULTILINE)
    displays = []
    pattern = re.compile(
        r"^\s*(\d+):\s+.+?\s+(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)(?:\s.*)?$"
    )
    for line in out.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        screen, w, h, x, y = map(int, match.groups())
        if w > 0 and h > 0:
            displays.append({"id": screen, "obs_screen": screen,
                             "x": x, "y": y, "w": w, "h": h,
                             "retina_scale": 1.0})

    if (header is None or int(header.group(1)) != len(displays)
            or len({d["obs_screen"] for d in displays}) != len(displays)):
        obs.script_log(obs.LOG_WARNING,
                       "Click Pop: invalid active RandR monitor list; "
                       "automatic display matching is unavailable")
        return []
    return displays


def _detect_screen_size():
    """Return (width, height) of the primary screen, or (1920, 1080).

    Thin wrapper for backward compatibility — uses the first display from
    ``_detect_all_displays()``.
    """
    global _retina_scale, _all_displays
    _all_displays = _detect_all_displays()
    if _all_displays:
        d = _all_displays[0]
        _retina_scale = d.get("retina_scale", 1.0)
        return (d["w"], d["h"])
    return (1920, 1080)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_listener = None          # pynput Listener thread
_click_queue = deque()    # thread‑safe (deque.append / popleft are atomic in CPython)
_timer_active = False
_active_clicks = []       # list of (source_name, expire_time)
_retina_scale = 1.0       # macOS Retina backing scale factor (2.0 on HiDPI)
_all_displays = []        # list of display descriptors from _detect_all_displays()
_captured_display = None  # display dict for the monitor being captured (or None)
_display_capture_map = {}    # {display_id: {"display": dict, "source_name": str}}
_multi_capture_mode = False  # True when "(all)" is selected

# Label used in the editable combo for multi-capture mode.
# OBS_COMBO_TYPE_EDITABLE stores the label text as the setting value,
# NOT the programmatic value parameter, so we must match on this label.
_ALL_CAPTURES_LABEL = "(all - auto detect)"

# Settings with defaults
_settings = {
    "left_image": "",
    "right_image": "",
    "duration_ms": 350,
    "circle_size": 60,
    "monitor_w": 1920,
    "monitor_h": 1080,
    "max_circles": 5,
    "capture_source": "",
    "override_monitor": False,
}

# ---------------------------------------------------------------------------
# OBS Script Boilerplate
# ---------------------------------------------------------------------------

def script_description():
    return (
        "<h2>Click Pop</h2>"
        "<p>Renders a circle in the OBS scene on every mouse click — "
        "visible only in recordings / streams, <b>not</b> on the actual desktop.</p>"
        "<p>Requires the <code>pynput</code> Python package.</p>"
    )


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_path(
        props, "left_image", "Left‑click image",
        obs.OBS_PATH_FILE, "PNG (*.png)", None,
    )
    obs.obs_properties_add_path(
        props, "right_image", "Right‑click image",
        obs.OBS_PATH_FILE, "PNG (*.png)", None,
    )
    obs.obs_properties_add_int(
        props, "duration_ms", "Circle duration (ms)", 100, 2000, 50,
    )
    obs.obs_properties_add_int(
        props, "circle_size", "Circle diameter (px)", 20, 300, 5,
    )

    override_prop = obs.obs_properties_add_bool(
        props, "override_monitor", "Override monitor dimensions",
    )
    obs.obs_property_set_modified_callback(override_prop, _on_override_toggle)

    p_w = obs.obs_properties_add_int(
        props, "monitor_w", "Monitor width (px)", 640, 7680, 1,
    )
    p_h = obs.obs_properties_add_int(
        props, "monitor_h", "Monitor height (px)", 480, 4320, 1,
    )
    # Hide manual dimension fields when override is off
    obs.obs_property_set_visible(p_w, _settings["override_monitor"])
    obs.obs_property_set_visible(p_h, _settings["override_monitor"])

    obs.obs_properties_add_int(
        props, "max_circles", "Max circles per click type", 1, 20, 1,
    )
    capture_list = obs.obs_properties_add_list(
        props, "capture_source",
        "Display Capture source (blank = no crop adjust)",
        obs.OBS_COMBO_TYPE_EDITABLE, obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(capture_list, "(none)", "")
    obs.obs_property_list_add_string(capture_list, _ALL_CAPTURES_LABEL, _ALL_CAPTURES_LABEL)
    _populate_capture_list(capture_list)
    obs.obs_properties_add_button(
        props, "btn_refresh", "Refresh Displays", _on_refresh_displays,
    )
    obs.obs_properties_add_button(
        props, "btn_start", "Start Listener", _on_start,
    )
    obs.obs_properties_add_button(
        props, "btn_stop", "Stop Listener", _on_stop,
    )

    # Show detected displays as informational text
    _add_display_info(props)

    return props


def _on_override_toggle(props, prop, settings):
    """Show/hide manual monitor dimension fields when checkbox is toggled."""
    override = obs.obs_data_get_bool(settings, "override_monitor")
    p_w = obs.obs_properties_get(props, "monitor_w")
    p_h = obs.obs_properties_get(props, "monitor_h")
    obs.obs_property_set_visible(p_w, override)
    obs.obs_property_set_visible(p_h, override)
    return True


def _add_display_info(props):
    """Add informational text showing detected displays."""
    if not _all_displays:
        return
    lines = ["Detected displays:"]
    for i, d in enumerate(_all_displays):
        retina = d.get("retina_scale", 1.0)
        label = f"  Display {i + 1}: {d['w']}x{d['h']} @ ({d['x']},{d['y']})"
        if retina != 1.0:
            label += f" [{retina:.0f}x Retina]"
        if _multi_capture_mode:
            info = _display_capture_map.get(d["id"])
            if info is not None:
                label += f" -> {info['source_name']}"
        elif _captured_display is d:
            label += " [CAPTURED]"
        lines.append(label)
    obs.obs_properties_add_text(
        props, "_display_info", "\n".join(lines), obs.OBS_TEXT_INFO,
    )


def script_defaults(settings):
    here = os.path.dirname(os.path.abspath(__file__))
    obs.obs_data_set_default_string(
        settings, "left_image", os.path.join(here, "click_circle.png"),
    )
    obs.obs_data_set_default_string(
        settings, "right_image", os.path.join(here, "click_circle_right.png"),
    )
    obs.obs_data_set_default_int(settings, "duration_ms", 350)
    obs.obs_data_set_default_int(settings, "circle_size", 60)
    mon_w, mon_h = _detect_screen_size()
    obs.obs_data_set_default_int(settings, "monitor_w", mon_w)
    obs.obs_data_set_default_int(settings, "monitor_h", mon_h)
    obs.obs_data_set_default_int(settings, "max_circles", 5)
    obs.obs_data_set_default_string(settings, "capture_source", "")
    obs.obs_data_set_default_bool(settings, "override_monitor", False)


def script_update(settings):
    _settings["left_image"] = obs.obs_data_get_string(settings, "left_image")
    _settings["right_image"] = obs.obs_data_get_string(settings, "right_image")
    _settings["duration_ms"] = obs.obs_data_get_int(settings, "duration_ms")
    _settings["circle_size"] = obs.obs_data_get_int(settings, "circle_size")
    _settings["override_monitor"] = obs.obs_data_get_bool(settings, "override_monitor")
    _settings["monitor_w"] = obs.obs_data_get_int(settings, "monitor_w")
    _settings["monitor_h"] = obs.obs_data_get_int(settings, "monitor_h")
    _settings["max_circles"] = obs.obs_data_get_int(settings, "max_circles")
    _settings["capture_source"] = obs.obs_data_get_string(settings, "capture_source")
    # Re-resolve which display is being captured when settings change
    _refresh_displays()
    # Auto-set monitor dimensions from captured display when not overridden
    if not _settings["override_monitor"]:
        if _captured_display is not None:
            _settings["monitor_w"] = _captured_display["w"]
            _settings["monitor_h"] = _captured_display["h"]


def script_unload():
    _stop_listener()
    _cleanup_sources()


# ---------------------------------------------------------------------------
# Listener management
# ---------------------------------------------------------------------------

def _refresh_displays():
    """Re-enumerate displays and resolve the captured display."""
    global _all_displays, _retina_scale
    _all_displays = _detect_all_displays()
    # Update _retina_scale from the primary display for backward compat
    if _all_displays:
        _retina_scale = _all_displays[0].get("retina_scale", 1.0)
    try:
        _resolve_captured_display()
    except Exception:
        pass
    # Log detected configuration for debugging multi-monitor issues
    for i, d in enumerate(_all_displays):
        dev = d.get('device_name', '')
        path = d.get('device_path', '')
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: display {i}: {d['w']}x{d['h']} "
                       f"@ ({d['x']},{d['y']}) retina={d.get('retina_scale',1.0)}"
                       f"{' dev=' + dev if dev else ''}"
                       f"{' path=' + path if path else ''}")
    if _multi_capture_mode:
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: multi-capture mode — "
                       f"{len(_display_capture_map)} display(s) mapped")
        for did, info in _display_capture_map.items():
            d = info["display"]
            obs.script_log(obs.LOG_INFO,
                           f"Click Pop:   display {did}: "
                           f"{d['w']}x{d['h']} -> {info['source_name']}")
    elif _captured_display:
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: captured display: "
                       f"{_captured_display['w']}x{_captured_display['h']} "
                       f"@ ({_captured_display['x']},{_captured_display['y']})")
    elif _settings.get("capture_source", "") not in ("", "(none)"):
        obs.script_log(obs.LOG_WARNING,
                       "Click Pop: selected capture's monitor could not be "
                       "resolved; indicators will be skipped. Check the "
                       "capture selection and Refresh Displays.")
    else:
        obs.script_log(obs.LOG_INFO,
                       "Click Pop: no capture selected — clicks use "
                       "per-monitor full-canvas mapping")


def _on_refresh_displays(props, prop):
    _refresh_displays()
    # Repopulate the Display Capture source dropdown
    capture_list = obs.obs_properties_get(props, "capture_source")
    if capture_list is not None:
        obs.obs_property_list_clear(capture_list)
        obs.obs_property_list_add_string(capture_list, "(none)", "")
        obs.obs_property_list_add_string(capture_list, _ALL_CAPTURES_LABEL, _ALL_CAPTURES_LABEL)
        _populate_capture_list(capture_list)
    n = len(_all_displays)
    if _multi_capture_mode:
        cap = f"all ({len(_display_capture_map)} mapped)"
    elif _captured_display:
        cap = "yes"
    else:
        cap = "no"
    obs.script_log(obs.LOG_INFO,
                   f"Click Pop: refreshed — {n} display(s), captured={cap}")
    return True


def _on_start(props, prop):
    _start_listener()
    return True


def _on_stop(props, prop):
    _stop_listener()
    return True


def _start_listener():
    global _listener, _timer_active
    if _listener is not None:
        return  # already running

    try:
        from pynput.mouse import Listener, Button
    except ImportError:
        obs.script_log(obs.LOG_ERROR, "pynput is not installed. Run: pip install pynput")
        return

    def on_click(x, y, button, pressed):
        if pressed:
            is_left = (button == Button.left)
            _click_queue.append((x, y, is_left, time.time()))

    _listener = Listener(on_click=on_click)
    _listener.daemon = True
    _listener.start()

    if not _timer_active:
        obs.timer_add(_poll_clicks, 16)  # ~60 fps polling
        _timer_active = True

    obs.script_log(obs.LOG_INFO, "Click Pop: listener started")


def _stop_listener():
    global _listener, _timer_active
    if _listener is not None:
        _listener.stop()
        _listener = None

    if _timer_active:
        obs.timer_remove(_poll_clicks)
        _timer_active = False

    obs.script_log(obs.LOG_INFO, "Click Pop: listener stopped")


# ---------------------------------------------------------------------------
# Display Capture detection — crop / position / scale
# ---------------------------------------------------------------------------

_DISPLAY_CAPTURE_PREFIXES = (
    "xshm_input",         # Linux X11 (xshm_input, xshm_input_v2, …)
    "monitor_capture",    # Windows
    "screen_capture",     # macOS (ScreenCaptureKit)
    "display_capture",    # macOS (legacy)
)


def _iter_display_capture_names():
    """Yield names of Display Capture sources in the current scene.

    Uses ``obs_scene_save_transform_states`` to discover source names (avoids
    ``obs_scene_enum_items`` which has a broken SWIG wrapper in OBS ≤32.0.x).
    """
    import json
    scene_src = obs.obs_frontend_get_current_scene()
    if scene_src is None:
        return
    try:
        scene = obs.obs_scene_from_source(scene_src)
        if scene is None:
            return
        data = obs.obs_scene_save_transform_states(scene, True)
        try:
            json_str = obs.obs_data_get_json(data)
        finally:
            obs.obs_data_release(data)
        if not json_str:
            return

        parsed = json.loads(json_str)
        seen = set()
        for scene_info in parsed.get("scenes_and_groups", []):
            group_source = None
            try:
                item_scene = scene
                if scene_info.get("is_group"):
                    group_name = scene_info.get("scene_name")
                    if not group_name:
                        continue
                    group_source = obs.obs_get_source_by_name(group_name)
                    if group_source is None:
                        continue
                    item_scene = obs.obs_group_from_source(group_source)
                    if item_scene is None:
                        continue

                # Scene-item IDs are local to each scene/group, not global.
                for item_info in scene_info.get("items", []):
                    item_id = item_info.get("id")
                    if item_id is None:
                        continue
                    item = obs.obs_scene_find_sceneitem_by_id(item_scene, item_id)
                    if item is None:
                        continue
                    source = obs.obs_sceneitem_get_source(item)
                    src_id = obs.obs_source_get_unversioned_id(source)
                    if src_id.startswith(_DISPLAY_CAPTURE_PREFIXES):
                        name = obs.obs_source_get_name(source)
                        if name not in seen:
                            seen.add(name)
                            yield name
            finally:
                if group_source is not None:
                    obs.obs_source_release(group_source)
    finally:
        obs.obs_source_release(scene_src)


def _populate_capture_list(prop):
    """Add current-scene Display Capture sources to a combo-box property."""
    try:
        for name in _iter_display_capture_names():
            obs.obs_property_list_add_string(prop, name, name)
    except Exception as exc:
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: capture list populate failed: {exc}")


def _get_filter_crop(source):
    """Sum the enabled Crop/Pad filters' offsets on *source*.

    Iterates the source's filter list via ``obs_source_backup_filters``
    (avoids the broken callback-based ``obs_source_enum_filters``).

    Returns ``(left, top, right, bottom)``.  Left/top include both relative
    and absolute crops; right/bottom total only explicit relative edge
    settings, not the output-size changes from absolute crops.  Coordinate
    mapping uses left/top; OBS supplies the filtered output dimensions.
    """
    import json
    filters = obs.obs_source_backup_filters(source)
    left = top = right = bottom = 0
    try:
        count = obs.obs_data_array_count(filters)
        for i in range(count):
            fdata = obs.obs_data_array_item(filters, i)
            try:
                fjson = obs.obs_data_get_json(fdata)
            finally:
                obs.obs_data_release(fdata)
            if not fjson:
                continue
            fobj = json.loads(fjson)
            if fobj.get("id") != "crop_filter" or not fobj.get("enabled", True):
                continue
            settings = fobj.get("settings", {})
            left += settings.get("left", 0)
            top += settings.get("top", 0)
            if settings.get("relative", True):
                right += settings.get("right", 0)
                bottom += settings.get("bottom", 0)
    finally:
        obs.obs_data_array_release(filters)
    return (left, top, right, bottom)


def _display_uuid_via_ctypes(display_id):
    """Get the UUID string for a CGDirectDisplayID using ctypes.

    PyObjC doesn't expose ``CGDisplayCreateUUIDFromDisplayID`` in all
    environments (notably the Python bundled with OBS), so we call the
    CoreGraphics C function directly via ctypes.

    Returns a UUID string like ``"09FA8E3F-DD10-3AB8-E04B-86F97A791ED1"``
    or ``None`` on failure.
    """
    import ctypes

    # CGDisplayCreateUUIDFromDisplayID lives in the ColorSync framework
    # (not CoreGraphics) on modern macOS.
    cs = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ColorSync.framework/ColorSync")
    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    cs.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
    cs.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p

    cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFUUIDCreateString.restype = ctypes.c_void_p

    cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p

    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long

    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool

    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None

    kCFStringEncodingUTF8 = 0x08000100

    uuid_ref = cs.CGDisplayCreateUUIDFromDisplayID(
        ctypes.c_uint32(int(display_id)))
    if not uuid_ref:
        return None

    str_ref = cf.CFUUIDCreateString(None, uuid_ref)
    if not str_ref:
        cf.CFRelease(uuid_ref)
        return None

    result = None
    c_str = cf.CFStringGetCStringPtr(str_ref, kCFStringEncodingUTF8)
    if c_str:
        result = c_str.decode("utf-8")
    else:
        length = cf.CFStringGetLength(str_ref)
        buf = ctypes.create_string_buffer(length * 4 + 1)
        if cf.CFStringGetCString(str_ref, buf, len(buf), kCFStringEncodingUTF8):
            result = buf.value.decode("utf-8")

    cf.CFRelease(str_ref)
    cf.CFRelease(uuid_ref)
    return result


def _resolve_display_for_source(source_name):
    """Resolve which physical display a named capture source records.

    Returns the matching display dict from ``_all_displays``, or ``None``.
    """
    if not source_name or not _all_displays:
        return None

    source = obs.obs_get_source_by_name(source_name)
    if source is None:
        return None

    src_id = obs.obs_source_get_unversioned_id(source)
    settings = obs.obs_source_get_settings(source)

    try:
        if sys.platform == "darwin":
            if src_id.startswith("screen_capture"):
                display_val = obs.obs_data_get_int(settings, "display")
                display_uuid = obs.obs_data_get_string(settings, "display_uuid")
                obs.script_log(obs.LOG_INFO,
                               f"Click Pop: resolving screen_capture — "
                               f"src_id={src_id!r}, display={display_val}, "
                               f"display_uuid={display_uuid!r}, "
                               f"known IDs={[int(d['id']) for d in _all_displays]}")

                # 1. Try UUID match via ctypes (primary method for OBS 30+)
                if display_uuid:
                    norm_uuid = display_uuid.strip("{}").upper()
                    try:
                        for d in _all_displays:
                            d_uuid = _display_uuid_via_ctypes(d["id"])
                            if d_uuid and d_uuid.strip("{}").upper() == norm_uuid:
                                obs.script_log(obs.LOG_INFO,
                                               f"Click Pop: matched display by UUID")
                                return d
                    except Exception as exc:
                        obs.script_log(obs.LOG_INFO,
                                       f"Click Pop: UUID matching failed: {exc}")

                # 2. Fall back to CGDirectDisplayID match
                if display_val:
                    for d in _all_displays:
                        if int(d["id"]) == int(display_val):
                            obs.script_log(obs.LOG_INFO,
                                           f"Click Pop: matched display by ID")
                            return d

                # 3. Fall back to source output dimensions
                src_w = obs.obs_source_get_width(source)
                src_h = obs.obs_source_get_height(source)
                if src_w and src_h:
                    for d in _all_displays:
                        phys_w = int(d["w"] * d.get("retina_scale", 1.0))
                        phys_h = int(d["h"] * d.get("retina_scale", 1.0))
                        if phys_w == src_w and phys_h == src_h:
                            obs.script_log(obs.LOG_INFO,
                                           f"Click Pop: matched display by "
                                           f"dimensions {src_w}x{src_h}")
                            return d

                obs.script_log(obs.LOG_INFO,
                               "Click Pop: screen_capture display match failed")
            elif src_id.startswith("display_capture"):
                # Legacy display_capture: "display" is a 0-based index
                display_idx = obs.obs_data_get_int(settings, "display")
                if 0 <= display_idx < len(_all_displays):
                    return _all_displays[display_idx]
        elif sys.platform == "win32":
            # OBS 28+ stores monitor_id as a PnP device interface path
            # (e.g. \\?\DISPLAY#HW_ID#INSTANCE#{GUID}).
            # Older OBS used a 0-based int "monitor".
            monitor_id = obs.obs_data_get_string(settings, "monitor_id")
            monitor_idx = obs.obs_data_get_int(settings, "monitor")
            obs.script_log(obs.LOG_INFO,
                           f"Click Pop: resolving monitor_capture — "
                           f"monitor_id={monitor_id!r}, "
                           f"monitor={monitor_idx}")

            # 1. Match by device interface path (OBS 28+)
            if monitor_id:
                for d in _all_displays:
                    if d.get("device_path") == monitor_id:
                        obs.script_log(obs.LOG_INFO,
                                       f"Click Pop: matched display by "
                                       f"device path")
                        return d

            # 2. Fall back to monitor index (legacy OBS)
            if 0 <= monitor_idx < len(_all_displays):
                return _all_displays[monitor_idx]
        elif src_id.startswith("xshm_input"):
            # OBS uses RandR's active-monitor IDs, not connector/list order.
            # Synthetic fallback displays intentionally have no obs_screen.
            screen_idx = obs.obs_data_get_int(settings, "screen")
            for d in _all_displays:
                if d.get("obs_screen") == screen_idx:
                    return d
    except Exception as exc:
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: _resolve_display_for_source error: {exc}")
    finally:
        obs.obs_data_release(settings)
        obs.obs_source_release(source)

    return None


def _resolve_all_capture_sources():
    """Populate ``_display_capture_map`` for all Display Capture sources.

    Enumerates Display Capture sources in the current scene and resolves
    which physical display each captures.
    """
    global _display_capture_map
    _display_capture_map = {}

    try:
        source_names = list(_iter_display_capture_names())
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: _resolve_all found display capture sources: "
                       f"{source_names}")
        for name in source_names:
            display = _resolve_display_for_source(name)
            obs.script_log(obs.LOG_INFO,
                           f"Click Pop: _resolve_all source={name!r} -> "
                           f"display={display['w']}x{display['h']}@"
                           f"({display['x']},{display['y']}) id={display['id']}"
                           if display else
                           f"Click Pop: _resolve_all source={name!r} -> None")
            if display is not None:
                _display_capture_map[display["id"]] = {
                    "display": display,
                    "source_name": name,
                }
    except Exception as exc:
        obs.script_log(obs.LOG_INFO,
                       f"Click Pop: _resolve_all_capture_sources error: {exc}")


def _resolve_captured_display():
    """Determine which physical display the selected capture source records.

    In single-source mode, sets ``_captured_display`` to the matching
    display dict.  In multi-capture mode, populates
    ``_display_capture_map`` instead.
    """
    global _captured_display, _multi_capture_mode
    _captured_display = None
    _multi_capture_mode = False

    name = _settings.get("capture_source", "")
    if not name:
        return

    if name == _ALL_CAPTURES_LABEL:
        _multi_capture_mode = True
        _resolve_all_capture_sources()
        return

    _captured_display = _resolve_display_for_source(name)


def _find_capture_path(scene, name):
    """Return root-to-capture (borrowed item, saved state) pairs.

    OBS's recursive source lookup only searches one group level.  Its saved
    transform snapshot gives us each group's local item IDs without relying
    on the callback-based enumeration API in Python.
    """
    import json
    saved = obs.obs_scene_save_transform_states(scene, True)
    try:
        raw = obs.obs_data_get_json(saved)
        snapshot = json.loads(raw) if raw else {}
    finally:
        obs.obs_data_release(saved)
    scenes = {entry["scene_name"]: entry
              for entry in snapshot.get("scenes_and_groups", [])}

    def find(current):
        scene_name = obs.obs_source_get_name(obs.obs_scene_get_source(current))
        for state in scenes.get(scene_name, {}).get("items", []):
            item = obs.obs_scene_find_sceneitem_by_id(current, state["id"])
            if item is None:
                continue
            source = obs.obs_sceneitem_get_source(item)
            if obs.obs_source_get_name(source) == name:
                return [(item, state)]
            if obs.obs_sceneitem_is_group(item):
                path = find(obs.obs_sceneitem_group_get_scene(item))
                if path:
                    return [(item, state)] + path
        return None

    return find(scene)


def _get_source_crop_offset(source):
    """Source-property and enabled-filter crop preceding a scene item."""
    settings = obs.obs_source_get_settings(source)
    try:
        left = obs.obs_data_get_int(settings, "cut_left")
        top = obs.obs_data_get_int(settings, "cut_top")
    finally:
        obs.obs_data_release(settings)
    filter_left, filter_top, _, _ = _get_filter_crop(source)
    return left + filter_left, top + filter_top


def _get_bounds_crop_offset(source, crop, state):
    """Reproduce only OBS's hidden pre-draw Crop-to-Bounds texture offset.

    The native draw matrix includes bounds placement, but get_crop() omits
    this extra texture crop.  The snapshot exposes crop_to_bounds even on
    bindings whose obs_transform_info wrapper does not expose that field.
    """
    bounds_type = state.get("bounds_type")
    if not state.get("crop_to_bounds", False) or bounds_type not in (
            obs.OBS_BOUNDS_SCALE_OUTER, obs.OBS_BOUNDS_SCALE_TO_WIDTH,
            obs.OBS_BOUNDS_SCALE_TO_HEIGHT):
        return 0, 0

    width = obs.obs_source_get_width(source) - crop.left - crop.right
    height = obs.obs_source_get_height(source) - crop.top - crop.bottom
    width = 2 if width < 0 else width  # OBS calc_cx/calc_cy fallback
    height = 2 if height < 0 else height
    scale_x, scale_y = state["scale"]["x"], state["scale"]["y"]
    bounds_w, bounds_h = state["bounds"]["x"], state["bounds"]["y"]
    if not width or not height or not scale_x or not scale_y:
        return 0, 0
    width_ratio = bounds_w / (width * abs(scale_x))
    height_ratio = bounds_h / (height * abs(scale_y))
    if bounds_type == obs.OBS_BOUNDS_SCALE_TO_WIDTH:
        factor = width_ratio
    elif bounds_type == obs.OBS_BOUNDS_SCALE_TO_HEIGHT:
        factor = height_ratio
    else:
        factor = max(width_ratio, height_ratio)
    scale_x *= factor
    scale_y *= factor
    diff_x = bounds_w - width * abs(scale_x)
    diff_y = bounds_h - height * abs(scale_y)
    if diff_x < -0.1:
        diff, scale = diff_x, scale_x
        low, high = obs.OBS_ALIGN_LEFT, obs.OBS_ALIGN_RIGHT
    elif diff_y < -0.1:
        diff, scale = diff_y, scale_y
        low, high = obs.OBS_ALIGN_TOP, obs.OBS_ALIGN_BOTTOM
    else:
        return 0, 0

    overdraw = abs(diff / scale)
    alignment = state.get("bounds_alignment", 0)
    offset = 0 if alignment & low else overdraw if alignment & high else overdraw / 2
    if scale < 0:
        offset = overdraw - offset
    offset = int(offset + 0.5)  # OBS roundf, not Python's ties-to-even round
    return (offset, 0) if diff_x < -0.1 else (0, offset)


def _get_capture_transform(scene, source_name=None):
    """Return source crop and the full source-to-canvas affine mapping.

    Native draw transforms handle alignment, rotation, flips and all bounds
    modes.  Manual/automatic texture crops precede each item's draw matrix;
    enclosing groups are then applied from the capture out to the canvas.
    """
    name = source_name or _settings.get("capture_source", "")
    if scene is None or not name or name in ("(none)", _ALL_CAPTURES_LABEL):
        return None
    path = _find_capture_path(scene, name)
    if not path:
        return None

    source = obs.obs_sceneitem_get_source(path[-1][0])
    crop_left, crop_top = _get_source_crop_offset(source)
    transform = (1, 0, 0, 1, 0, 0)
    for depth, (item, state) in enumerate(reversed(path)):
        source = obs.obs_sceneitem_get_source(item)
        crop = obs.obs_sceneitem_crop()
        obs.obs_sceneitem_get_crop(item, crop)
        left, top = _get_bounds_crop_offset(source, crop, state)
        left += crop.left
        top += crop.top
        if depth:  # group filters run after its children have been composed
            group_left, group_top = _get_source_crop_offset(source)
            left += group_left
            top += group_top

        matrix = obs.matrix4()
        obs.obs_sceneitem_get_draw_transform(item, matrix)
        xx, xy, yx, yy = matrix.x.x, matrix.y.x, matrix.x.y, matrix.y.y
        tx = matrix.t.x - xx * left - xy * top
        ty = matrix.t.y - yx * left - yy * top
        a, b, c, d, e, f = transform
        transform = (xx * a + xy * c, xx * b + xy * d,
                     yx * a + yy * c, yx * b + yy * d,
                     xx * e + xy * f + tx, yx * e + yy * f + ty)

    return {"crop_left": crop_left, "crop_top": crop_top,
            "capture_transform": transform}


# ---------------------------------------------------------------------------
# OBS timer callback — runs on the UI thread
# ---------------------------------------------------------------------------

def _poll_clicks():
    now = time.time()
    duration_s = _settings["duration_ms"] / 1000.0

    # Drain new clicks from the queue
    while _click_queue:
        x, y, is_left, t = _click_queue.popleft()
        _spawn_circle(x, y, is_left, t + duration_s)

    # Expire old circles
    still_active, expired = expire_circles(_active_clicks, now)
    for name in expired:
        _hide_source(name)
    _active_clicks[:] = still_active


def _spawn_circle(x, y, is_left, expire_time):
    """Create or reuse an image source and position it at (x, y).

    Coordinates (x, y) are in virtual-desktop space (as reported by pynput).
    Multi-monitor aware: determines which display was clicked, converts to
    display-local coordinates, and discards clicks on non-captured displays.
    """
    # Routing and display-local coordinates also matter with just one
    # monitor: it can have a nonzero origin or a cropped/scaled capture.
    display = find_display_for_point(x, y, _all_displays)
    capture_source_name = None
    selected = _settings.get("capture_source", "")
    if _multi_capture_mode:
        info = _display_capture_map.get(display["id"]) if display else None
        if info is None:
            return
        capture_source_name = info["source_name"]
    else:
        if selected not in ("", "(none)") and _captured_display is None:
            return  # a capture transform cannot tell us its monitor's origin
        if _captured_display is not None and display is not _captured_display:
            return

    if display is not None:
        local_x = x - display["x"]
        local_y = y - display["y"]
        retina = display.get("retina_scale", 1.0)
    else:
        local_x = x
        local_y = y
        retina = _retina_scale

    if display is not None and not _settings["override_monitor"]:
        mon_w = display["w"]
        mon_h = display["h"]
    else:
        mon_w = _settings["monitor_w"]
        mon_h = _settings["monitor_h"]
    size = _settings["circle_size"]

    # Map mouse coords to canvas coords before allocating a source.  A
    # selected capture missing from this scene must not produce a guessed
    # position or evict an existing indicator.
    scene_src = obs.obs_frontend_get_current_scene()
    if scene_src is None:
        return
    try:
        scene = obs.obs_scene_from_source(scene_src)
        if scene is None:
            return
        transform = _get_capture_transform(scene, capture_source_name)
        if transform is None and (capture_source_name or selected not in ("", "(none)")):
            return

        canvas_w = obs.obs_source_get_width(scene_src) or mon_w
        canvas_h = obs.obs_source_get_height(scene_src) or mon_h
        # Retina clicks are logical points; capture transforms use pixels.
        # With no selection, map this monitor only, not the whole desktop.
        obs_x, obs_y = map_coords(
            local_x * retina, local_y * retina, canvas_w, canvas_h,
            mon_w * retina, mon_h * retina, size, **(transform or {}),
        )
    finally:
        obs.obs_source_release(scene_src)

    prefix = "__click_pop_L_" if is_left else "__click_pop_R_"
    src_name, evicted = allocate_slot(prefix, _settings["max_circles"], _active_clicks)
    if evicted is not None:
        _hide_source(evicted)
    image_path = _settings["left_image"] if is_left else _settings["right_image"]
    _show_source(src_name, image_path, obs_x, obs_y, size)
    _active_clicks.append((src_name, expire_time))


# ---------------------------------------------------------------------------
# OBS Source helpers
# ---------------------------------------------------------------------------

def _get_current_scene():
    scene_source = obs.obs_frontend_get_current_scene()
    # obs_scene_from_source does not increment the ref count — no release needed for scene
    scene = obs.obs_scene_from_source(scene_source)
    obs.obs_source_release(scene_source)
    return scene


def _show_source(name, image_path, x, y, size):
    scene = _get_current_scene()
    if scene is None:
        return

    scene_item = obs.obs_scene_find_source(scene, name)

    if scene_item is None:
        # Source not in scene — check if it exists globally (e.g. from a
        # previous session) and reuse it, otherwise create a new one.
        source = obs.obs_get_source_by_name(name)
        if source is None:
            settings = obs.obs_data_create()
            obs.obs_data_set_string(settings, "file", image_path)
            source = obs.obs_source_create("image_source", name, settings, None)
            obs.obs_data_release(settings)
        else:
            # Update image path on reused source (may be stale from a
            # previous session or OBS restart).
            settings = obs.obs_source_get_settings(source)
            obs.obs_data_set_string(settings, "file", image_path)
            obs.obs_source_update(source, settings)
            obs.obs_data_release(settings)
        scene_item = obs.obs_scene_add(scene, source)
        obs.obs_source_release(source)
    else:
        # Update the image path in case it changed
        source = obs.obs_sceneitem_get_source(scene_item)
        settings = obs.obs_source_get_settings(source)
        obs.obs_data_set_string(settings, "file", image_path)
        obs.obs_source_update(source, settings)
        obs.obs_data_release(settings)

    # Position and scale
    pos = obs.vec2()
    pos.x = x
    pos.y = y
    obs.obs_sceneitem_set_pos(scene_item, pos)

    # Scale the source to the desired circle size
    source = obs.obs_sceneitem_get_source(scene_item)
    src_w = obs.obs_source_get_width(source)
    if src_w and src_w > 0:
        s = size / src_w
        scale = obs.vec2()
        scale.x = s
        scale.y = s
        obs.obs_sceneitem_set_scale(scene_item, scale)

    obs.obs_sceneitem_set_visible(scene_item, True)


def _hide_source(name):
    scene = _get_current_scene()
    if scene is None:
        return
    scene_item = obs.obs_scene_find_source(scene, name)
    if scene_item is not None:
        obs.obs_sceneitem_set_visible(scene_item, False)


def _cleanup_sources():
    """Remove all __click_pop_* sources from the current scene on unload."""
    scene = _get_current_scene()
    if scene is None:
        return
    max_c = _settings["max_circles"]
    for prefix in ("__click_pop_L_", "__click_pop_R_"):
        for i in range(max_c):
            name = f"{prefix}{i}"
            scene_item = obs.obs_scene_find_source(scene, name)
            if scene_item is not None:
                obs.obs_sceneitem_remove(scene_item)
            source = obs.obs_get_source_by_name(name)
            if source is not None:
                obs.obs_source_remove(source)
                obs.obs_source_release(source)
    _active_clicks.clear()
