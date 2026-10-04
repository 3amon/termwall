"""Move + activate the termwall kitty window (X11 only, via EWMH requests).

mutter puts new windows on whichever monitor it likes and may refuse focus
requests from background apps; asking as a "pager" (source indication 2)
for _NET_ACTIVE_WINDOW is honoured.
"""
from __future__ import annotations

import os
import time

try:
    from Xlib import X, display
    from Xlib.protocol import event
except ImportError:  # not on X11 / not installed
    display = None


def _find(d, wm_class: str):
    root = d.screen().root
    ids = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    for wid in (ids.value if ids else []):
        w = d.create_resource_object("window", wid)
        try:
            cls = w.get_wm_class()
        except Exception:
            continue
        if cls and wm_class in cls:
            return w
    return None


def _send(d, w, atom: str, data):
    root = d.screen().root
    ev = event.ClientMessage(window=w, client_type=d.intern_atom(atom),
                             data=(32, (list(data) + [0] * 5)[:5]))
    root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)


def _frame_geometry(d, w):
    g = w.get_geometry()
    pos = w.translate_coords(d.screen().root, 0, 0)
    return -pos.x, -pos.y, g.width, g.height


def present(wm_class: str, screen: tuple[int, int, int, int] | None, wait: float = 3.0) -> bool:
    """Put the window on `screen` (x, y, w, h), maximized, and activate it."""
    if display is None or not os.environ.get("DISPLAY"):
        return False
    d = display.Display()
    try:
        deadline = time.time() + wait
        w = _find(d, wm_class)
        while w is None and time.time() < deadline:
            time.sleep(0.05)
            w = _find(d, wm_class)
        if w is None:
            return False
        if screen:
            sx, sy, sw, sh = screen
            x, y, ww, wh = _frame_geometry(d, w)
            cx, cy = x + ww // 2, y + wh // 2
            if not (sx <= cx < sx + sw and sy <= cy < sy + sh):
                horz = d.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
                vert = d.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT")
                _send(d, w, "_NET_WM_STATE", [0, horz, vert, 1])          # unmaximize
                d.sync()
                w.configure(x=sx + 40, y=sy + 40, width=min(ww, sw - 80), height=min(wh, sh - 80))
                d.sync()
                time.sleep(0.05)
                _send(d, w, "_NET_WM_STATE", [1, horz, vert, 1])          # maximize there
        _send(d, w, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
        d.sync()
        return True
    finally:
        d.close()
