"""Cheap check (no crypto imports) used by the desktop daemon: has a phone subscribed to app push?"""
import json

from .config import CONFIG_DIR


def app_push_enabled() -> bool:
    try:
        from .config import load
        if load().notify.get("phone_via") == "app":   # app server runs on another host
            return True
    except Exception:
        pass
    try:
        return bool(json.loads((CONFIG_DIR / "push_subscriptions.json").read_text()))
    except (OSError, ValueError):
        return False
