"""Web Push for the termwall PWA (iOS 16.4+ delivers it to home-screen apps).

VAPID keys and subscriptions live in ~/.config/termwall. Same policy as ntfy:
pushed while you're away from the desk machine, approvals only unless phone = "all".
Payloads are end-to-end encrypted to the device; we still keep them short.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys

from aiohttp import web
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from . import config, presence

KEY_PATH = config.CONFIG_DIR / "vapid_private.pem"
SUBS_PATH = config.CONFIG_DIR / "push_subscriptions.json"
# Apple's push service rejects VAPID tokens whose subject is a localhost mailto (BadJwtToken).
# Placeholder: set [web] vapid_subject in config.toml to an address you control.
VAPID_SUBJECT = "mailto:termwall@example.net"


def subscriptions() -> list[dict]:
    try:
        return json.loads(SUBS_PATH.read_text())
    except (OSError, ValueError):
        return []


def app_push_enabled() -> bool:
    """True once a phone subscribed: the desktop daemon then leaves phone alerts to us."""
    return bool(subscriptions())


class Push:
    def __init__(self, app):
        self.app = app
        if not KEY_PATH.exists():
            v = Vapid()
            v.generate_keys()
            v.save_key(str(KEY_PATH))
            KEY_PATH.chmod(0o600)
        self.vapid = Vapid.from_file(str(KEY_PATH))
        raw = self.vapid.public_key.public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)
        self.public = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def routes(self, r):
        r.add_get("/api/push/key", self.h_key)
        r.add_post("/api/push/subscribe", self.h_subscribe)
        r.add_post("/api/push/test", self.h_test)

    async def h_key(self, request):
        return web.json_response({"key": self.public})

    async def h_subscribe(self, request):
        sub = await request.json()
        if not sub.get("endpoint"):
            return web.json_response({"ok": False, "error": "bad subscription"})
        subs = [s for s in subscriptions() if s.get("endpoint") != sub["endpoint"]] + [sub]
        SUBS_PATH.write_text(json.dumps(subs))
        SUBS_PATH.chmod(0o600)
        print("push: subscribed", sub["endpoint"].split("/")[2], file=sys.stderr, flush=True)
        await self.send({"title": "termwall", "body": "Notifications are on. Approvals reach you here while you're away."},
                        only=[sub])
        return web.json_response({"ok": True})

    async def h_test(self, request):
        n = await self.send({"title": "termwall: test", "body": "Push works."})
        return web.json_response({"ok": n > 0, "sent": n})

    async def notify(self, kind: str, s):
        n = self.app["cfg"].notify
        phone = n.get("phone", "approvals")
        if phone == "never" or (phone == "approvals" and kind != "approval"):
            return
        if not await self.away(float(n.get("away_minutes", 5))):
            return
        title = "⚠ needs approval" if kind == "approval" else "✓ finished"
        await self.send({"title": title, "body": f"{s.host} · {s.name or s.title or s.agent}", "key": s.key})

    async def away(self, minutes: float) -> bool:
        """Away from the desk machine (notify.desk_host, default the first host). The
        server may run elsewhere (an always-on machine), so ask that host's agent. Unreachable = away."""
        cfg = self.app["cfg"]
        desk = cfg.notify.get("desk_host") or cfg.hosts[0].name
        h = self.app["hosts"].get(desk)
        if h is None:
            return await asyncio.to_thread(presence.away, minutes)
        r = await h.call("presence", timeout=8)
        if not r.get("ok"):
            return True
        return bool(r.get("locked")) or r.get("idle", 0) >= minutes * 60

    async def send(self, payload: dict, only: list[dict] | None = None) -> int:
        subs = only if only is not None else subscriptions()
        sent, dead = 0, []
        for sub in subs:
            try:
                await asyncio.to_thread(
                    webpush, subscription_info=sub, data=json.dumps(payload),
                    vapid_private_key=str(KEY_PATH), vapid_claims={"sub": self.app["cfg"].web.get("vapid_subject") or VAPID_SUBJECT}, ttl=3600)
                sent += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):
                    dead.append(sub.get("endpoint"))   # unsubscribed / expired
                print("push failed:", code, e, file=sys.stderr)
        if dead:
            SUBS_PATH.write_text(json.dumps([s for s in subscriptions() if s.get("endpoint") not in dead]))
        return sent
