def find_display_for_point(x, y, displays):
    """Return the display dict whose bounds contain (x, y), or None.

    Each display dict must have keys: x, y, w, h (origin and logical size).
    """
    for d in displays:
        if d["x"] <= x < d["x"] + d["w"] and d["y"] <= y < d["y"] + d["h"]:
            return d
    return None


def map_coords(x, y, canvas_w, canvas_h, monitor_w, monitor_h, circle_size,
               crop_left=0, crop_top=0, capture_transform=None):
    """Map mouse coordinates to OBS canvas coordinates, centered on the circle.

    *capture_transform* is an affine tuple ``(xx, xy, yx, yy, tx, ty)``
    mapping cropped source pixels into canvas space, including any groups;
    a plain position/scale is expressed as ``(sx, 0, 0, sy, px, py)``.
    Without it, map one full monitor proportionally onto the canvas.  Center
    the indicator only after the transformation so its diameter stays in
    canvas pixels.

    Returns (obs_x, obs_y).
    """
    cropped_x = x - crop_left
    cropped_y = y - crop_top
    if capture_transform is not None:
        xx, xy, yx, yy, tx, ty = capture_transform
        obs_x = xx * cropped_x + xy * cropped_y + tx
        obs_y = yx * cropped_x + yy * cropped_y + ty
    else:
        obs_x = cropped_x * canvas_w / monitor_w
        obs_y = cropped_y * canvas_h / monitor_h
    return (obs_x - circle_size / 2, obs_y - circle_size / 2)


def allocate_slot(prefix, max_circles, active_clicks):
    """Find a free slot or evict the oldest entry for *prefix*.

    *active_clicks* is a list of ``(source_name, expire_time)`` tuples and
    **may be mutated** (the evicted entry is removed in-place so the caller
    doesn't double-count it).

    Returns ``(slot_name, evicted_name | None)``.
    """
    # Try to find a free slot
    for i in range(max_circles):
        candidate = f"{prefix}{i}"
        in_use = any(n == candidate for n, _ in active_clicks)
        if not in_use:
            return (candidate, None)

    # All slots busy — evict the oldest matching this prefix
    for i, (n, _) in enumerate(active_clicks):
        if n.startswith(prefix):
            active_clicks.pop(i)
            return (n, n)

    # Fallback (shouldn't happen if active_clicks is consistent)
    return (f"{prefix}0", None)


def expire_circles(active_clicks, now):
    """Partition *active_clicks* into still-active and expired.

    Returns ``(still_active, expired_names)`` where *still_active* has the
    same ``(name, expire_time)`` shape and *expired_names* is a plain list
    of source names.
    """
    still_active = []
    expired_names = []
    for name, expire_t in active_clicks:
        if now >= expire_t:
            expired_names.append(name)
        else:
            still_active.append((name, expire_t))
    return (still_active, expired_names)
