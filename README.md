# Click Pop for OBS

Show mouse click indicators **only in your OBS recordings/streams** — invisible on the actual desktop.

By default, left clicks show a red circle and right clicks show a blue circle. Indicators disappear after a configurable duration. Replace the included PNGs with any transparent image to customize the shape, color, and style.

![Demo](obs-click-pop-demo.gif)

## Why?

Clear to the viewer, no clutter to the demonstrator.
Your viewers will see exactly where you clicked, but you don't see any click overlay in your working space.

## Features

- Left/right click differentiation with customizable indicator images
- Crop-aware coordinate mapping — works correctly when your Display Capture source is cropped via:
  - Source Properties (e.g. XSHM "Crop Left/Top")
  - Edit Transform / Alt-drag
  - Crop/Pad filter
- Auto-detection of Display Capture sources with an editable dropdown
- Correct handling of OBS bounding box scaling modes (Scale Inner, Stretch, etc.)
- Capture positioning follows OBS transforms, including moved/scaled groups
- Configurable circle size, duration, and max simultaneous circles
- Works on 4K monitors with scaled canvas output
- Supports multi-monitor setups
- Supports Retina displays
- Supports Windows, macOS, Linux (X11)

## Files

| File | Purpose |
|------|---------|
| `obs_click_pop.py` | Main OBS Python script |
| `click_pop_core.py` | Pure logic functions (coordinate mapping, slot allocation, expiration) |
| `click_circle.png` | Red circle for left clicks (80x80 transparent PNG) |
| `click_circle_right.png` | Blue circle for right clicks (80x80 transparent PNG) |
| `click_crosshair.png` | Red crosshair for left clicks (80x80 transparent PNG) |
| `click_crosshair_right.png` | Blue crosshair for right clicks (80x80 transparent PNG) |

## Setup

### Prerequisites

- **OBS Studio 30+** (tested with 32.0.2 and 32.0.4)
- **Python 3.12** (must match the version OBS was built against)
- **pynput** package (installed into the same Python — see platform steps below)

### Windows

1. Install Python 3.12: `winget install Python.Python.3.12`
2. Install pynput: `pip install pynput`
3. In OBS, go to **Tools > Scripts > Python Settings** and set the path to your Python install
   (e.g. `C:\Users\<you>\AppData\Local\Programs\Python\Python312`)

### Linux

On Linux, OBS is linked directly against the system Python at compile time — there is no **Python Settings** tab. Just install pynput into the matching system Python:

```bash
python3.12 -m pip install pynput
```

If your distro's OBS package was built against a different Python version, match that version instead.

### macOS

1. Install Python 3.12 and pynput: `python3.12 -m pip install pynput`
2. In OBS, go to **Tools > Scripts > Python Settings** and set the path to your Python install
3. Grant OBS (or Python) **Accessibility** permissions:
   System Settings > Privacy & Security > Accessibility > add OBS Studio

### Installation

1. Clone or download this repository to a permanent folder
2. In OBS, go to **Tools > Scripts**, click **+** and select `obs_click_pop.py`
3. Configure settings:
   - **Left/Right-click images** — what overlay graphic to use for left and right clicks. By default, red and blue circles are used.
   - **Circle duration** — how long each indicator stays visible (default 350 ms)
   - **Circle diameter** — size in pixels (default 60)
   - **Monitor width/height** — auto-detected; enable **Override monitor dimensions** to change the dimensions used by fallback mapping
   - **Max simultaneous circles** — how many indicators can show at once (default 5)
   - **Display Capture source** — select the capture being shown for accurate positioning, including crops, scaling, and groups. Use **(all - auto detect)** for one capture per monitor (also works with a single monitor).
4. Click **Start Listener**
5. Start recording — clicks will appear as colored circles in the output

After updating the Python files, restart OBS so both the main script and its
`click_pop_core.py` helper are reloaded. Then select your capture, click
**Refresh Displays**, and **Start Listener**.

### Cropped Display Capture

> **Changed behavior:** with the **Display Capture source** left blank or set to **(none)**, earlier versions scaled the *whole multi-monitor desktop* onto the canvas. Now the *clicked monitor* is mapped to the full canvas. If you relied on the old whole-desktop mapping, select your capture source (or **(all - auto detect)**) instead.

If your Display Capture source is cropped to a sub-region of your screen, select it from the **Display Capture source** dropdown. The script reads crop offsets and scale from the source properties, scene-item transform, and any Crop/Pad filters to map mouse coordinates correctly.

Disabled Crop/Pad filters are ignored; multiple enabled crop filters are combined. **Crop to Bounding Box** (OBS 30.1 and later) is honored; older OBS versions have no such option, so nothing is lost there.

Leaving the selection blank or choosing **(none)** uses a simpler fallback: it maps the clicked monitor to the full canvas. It does **not** map the entire multi-monitor desktop or account for a capture's crop, position, or aspect-ratio padding. Select a capture for those adjustments. If a selected capture is absent from the current scene, indicators are skipped instead of placed using a guessed transform. If its monitor cannot be identified, clicks are skipped on multi-monitor setups; with exactly one display detected, that display is assumed.

The capture's transform is re-read at most once per second while you click, so moving, scaling, or cropping the capture takes effect within a second without a restart.


## Customization

Replace the PNG files with your own designs. Any transparent PNG works — the script will scale it to the configured circle diameter.


## Tips

You need to click on "Refresh Displays":
- after you add or remove Screen Capture sources
- after you change monitor connections on your machine
- after you change a Screen Capture source's Display
- after switching scenes when using **(all - auto detect)**

On Linux, automatic monitor matching requires `xrandr --listactivemonitors` (RandR 1.5+). This uses the same active-monitor numbering as OBS XSHM; connector order from `xrandr --query` can be different. If detection fails, the script logs a warning instead of guessing a monitor ID.


## Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| Windows | Works | No special setup needed |
| macOS | Works | Requires Accessibility permission |
| Linux (X11) | Works | Standard X11 input capture |
| Linux (Wayland) | Limited | pynput cannot capture global input on pure Wayland; use X11 or XWayland |


## Known Limitations

- No drag visualization (only click points)
- Circle appears at click position instantly (no fade-in/fade-out animation)
- Geometry-changing source filters other than Crop/Pad (for example, Scale/Aspect Ratio) are not mapped. Scene-item and group transforms are supported.
- Crop to Bounding Box offsets require OBS 30.1+, where the option exists; the transform snapshot of earlier versions has no such field.


## How It Works

1. A `pynput` background thread listens for mouse clicks globally
2. Click events are queued and processed by an OBS timer callback (~60 fps)
3. For each click, an OBS Image Source is created/repositioned in the current scene
4. Mouse coordinates are mapped from monitor space to OBS canvas space, accounting for any crop and scale transforms on the Display Capture
5. After the configured duration, the source is hidden
6. The indicators are **composited into the OBS output** — they exist only in the recording


## Developer Notes

### Running Tests

```bash
python -m venv .venv
.venv/bin/pip install pytest          # Windows: .venv\Scripts\pip
.venv/bin/pytest tests/ -v -m "not e2e"  # Windows: .venv\Scripts\pytest
```

The test suite includes:
- **Tier 1** — Pure logic tests (coordinate mapping, slot allocation, circle expiration)
- **Tier 2** — Mock OBS integration tests (the click-to-canvas pipeline, monitor matching, crop filters, group transforms, ref management, visibility)
- **Native** — Marked `@pytest.mark.native`, deselected by default. These start headless libobs through the installed `obspython` bindings and compare the script's transform composition against real scene items, groups, crops, bounds modes and `vec3_transform`. Run them with:

  ```bash
  OBS_SCRIPTING_DIR=/usr/lib/x86_64-linux-gnu/obs-scripting .venv/bin/pytest -m native -v
  ```

  `OBS_SCRIPTING_DIR` is the directory holding `obspython.py` and `_obspython.so` (the Linux default above is tried automatically). Cases that need Crop to Bounding Box skip on libobs builds older than 30.1.
- **E2E stubs** — Marked `@pytest.mark.e2e`, skipped by default (require a running OBS instance)



## License

GPL-2.0
