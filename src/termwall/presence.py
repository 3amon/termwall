"""Are you at the machine? (screen lock + input idle time) and phone push via ntfy."""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
import urllib.request


def _gdbus(dest, path, method) -> str:
    r = subprocess.run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path,
                        "--method", method], capture_output=True, text=True, timeout=2)
    return r.stdout


def idle_seconds() -> float:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True).stdout
            m = re.search(r'"HIDIdleTime" = (\d+)', out)
            return int(m.group(1)) / 1e9 if m else 0.0
        out = _gdbus("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                     "org.gnome.Mutter.IdleMonitor.GetIdletime")
        m = re.search(r"(\d+)", out)
        return int(m.group(1)) / 1000.0 if m else 0.0
    except Exception:
        return 0.0


def screen_locked() -> bool:
    try:
        if sys.platform == "darwin":
            return False
        return "true" in _gdbus("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                "org.gnome.ScreenSaver.GetActive")
    except Exception:
        return False


def away(minutes: float) -> bool:
    return screen_locked() or idle_seconds() >= minutes * 60


def in_quiet_hours(spec: str, now: dt.datetime | None = None) -> bool:
    """spec like "23:00-08:00" (may wrap midnight); empty = never quiet."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", spec or "")
    if not m:
        return False
    now = now or dt.datetime.now()
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    t = now.hour * 60 + now.minute
    return a <= t < b if a <= b else (t >= a or t < b)


def push(server: str, topic: str, title: str, body: str, approval: bool) -> str | None:
    """ntfy publish. Keep the payload minimal: it leaves the machine."""
    if not topic:
        return "no ntfy_topic configured"
    req = urllib.request.Request(
        f"{server.rstrip('/')}/{topic}", data=body.encode(), method="POST",
        headers={"Title": title.encode("ascii", "replace").decode(),
                 "Tags": "warning" if approval else "white_check_mark",
                 "Priority": "high" if approval else "default"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
        return None
    except Exception as e:
        return repr(e)
