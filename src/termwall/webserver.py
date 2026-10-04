"""termwall web: the wall as a phone app (PWA) + a terminal bridge.

Runs as its own service (no GUI needed). Listens on localhost only; publish it to
your tailnet with `tailscale serve` (HTTPS, required for an installable PWA and
push). Every API/WebSocket call needs the pairing token as well, because this
can type into your shells.

  GET  /api/state            live + recent sessions, host status, Codex usage
  WS   /ws/state             the same, pushed on every change
  WS   /ws/term?key=...      attach a terminal (tmux attach in a pty; ssh for remote hosts)
  POST /api/reply|kill|rename|resume|new
  GET  /api/peek?key=  /api/search?q=
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import itertools
import json
import os
import pty
import re
import secrets
import shlex
import signal
import struct
import sys
import termios
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import config
from .agent import leaked_agent_var
from .model import ANSI_RE, Session, Store

WEB_DIR = Path(__file__).parent / "web"
ASSETS = Path(__file__).parent / "assets"
TOKEN_PATH = config.CONFIG_DIR / "web_token"
PAIR_PATH = config.CONFIG_DIR / "pair_code.json"
PAIR_TRIES = 5
AGENT_SRC = (Path(__file__).parent / "agent.py").read_bytes()


def token() -> str:
    if not TOKEN_PATH.exists():
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(secrets.token_urlsafe(24))
        TOKEN_PATH.chmod(0o600)
    return TOKEN_PATH.read_text().strip()


def ssh_base(dest: str) -> list[str]:
    config.SSH_DIR.mkdir(parents=True, exist_ok=True)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3", "-o", "ControlMaster=auto",
            "-o", f"ControlPath={config.SSH_DIR}/ssh-%C", "-o", "ControlPersist=30m", dest]


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not leaked_agent_var(k)}
    env.update(extra)
    return env


class WebStore(Store):
    """Shares state.json with the desktop daemon but never writes it (that daemon owns it)."""

    def save(self):
        pass

    def reload_state(self):
        try:
            fresh = json.loads(config.STATE_PATH.read_text())
        except (OSError, ValueError):
            return
        keys = ("opened", "seen") if self.prefs_host else ("opened", "seen", "pinned", "hidden", "named")
        for k in keys:
            if k in fresh:
                self.state[k] = fresh[k]
        # idle_at transitions are tracked in memory too; keep the newer of both
        for k, v in fresh.get("idle_at", {}).items():
            self.state["idle_at"][k] = max(v, self.state["idle_at"].get(k, 0))


class Host:
    """One streaming agent per host (asyncio twin of hosts.HostConn)."""

    def __init__(self, app, host: config.Host):
        self.app, self.host = app, host
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "connecting"
        self.info: dict = {}
        self.pending: dict[int, asyncio.Future] = {}
        self.rids = itertools.count(1)

    def argv(self, interval: float) -> list[str]:
        code = f"import base64;exec(compile(base64.b64decode('{base64.b64encode(AGENT_SRC).decode()}'),'termwall-agent','exec'))"
        if not self.host.ssh:
            return [sys.executable, "-u", "-c", code, "--stream", str(interval)]
        remote = (f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; python3 -u -c {shlex.quote(code)} "
                  f"--stream {interval}")
        return ssh_base(self.host.ssh) + ["-T", remote]

    async def run(self):
        backoff = 1.0
        while True:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv(2.0), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=clean_env(), limit=64 * 1024 * 1024)
                async for line in self.proc.stdout:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "snapshot":
                        backoff = 1.0
                        self.info = {k: msg.get(k) for k in ("platform", "tmux", "home")}
                        if self.state != "online":
                            self.state = "online"
                        self.app["store"].host_state[self.host.name] = "online"
                        self.app["store"].ingest(self.host.name, msg)
                        self.app["changed"].set()
                        self.app["events"].extend(self._drain())
                    elif msg.get("type") == "reply":
                        fut = self.pending.pop(msg.get("rid"), None)
                        if fut and not fut.done():
                            fut.set_result(msg)
                await self.proc.wait()
            except Exception as e:  # keep reconnecting whatever happens
                print(f"{self.host.name}: {e!r}", file=sys.stderr)
            self.state = "offline"
            self.app["store"].host_state[self.host.name] = "offline"
            self.app["changed"].set()
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_result({"ok": False, "error": "host disconnected"})
            self.pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _drain(self):
        st = self.app["store"]
        out = [("finished", s) for s in st.finished] + [("approval", s) for s in st.attention]
        st.finished, st.attention = [], []
        return out

    async def call(self, op: str, timeout=30, **kw) -> dict:
        if not self.proc or self.proc.returncode is not None or self.state != "online":
            return {"ok": False, "error": f"{self.host.name} is offline"}
        rid = next(self.rids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self.proc.stdin.write((json.dumps({"op": op, "rid": rid, **kw}) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            return {"ok": False, "error": "timed out"}

    def attach_argv(self, tmux_id: str) -> list[str]:
        tm = self.info.get("tmux") or "tmux"
        if not self.host.ssh:
            return [tm, "attach-session", "-t", tmux_id]
        return ssh_base(self.host.ssh) + ["-t", f"exec {shlex.quote(tm)} attach-session -t {shlex.quote(tmux_id)}"]


# ---------------------------------------------------------------- serialisation

def status_of(store: Store, s: Session) -> tuple[str, str]:
    if s.offline:
        return "offline", "offline"
    if s.kind == "history":
        return "ended", "ended · tap to resume"
    if s.status == "approval":
        return "approval", "needs approval"
    if s.agent == "shell":
        return "idle", s.cmd or "shell"
    if s.status == "busy":
        return "busy", "working"
    if store.is_stale(s):
        return "idle", f"stale · {int((time.time() - s.updated) // 86400)}d"
    if store.needs_you(s):
        return "waiting", "waiting for you"
    return "idle", "idle"


def screen_tail(screen: str, n=14) -> str:
    """Last n non-empty lines, ANSI colours kept (the app renders them)."""
    lines = screen.rstrip().split("\n")
    while lines and not ANSI_RE.sub("", lines[-1]).strip():
        lines.pop()
    return "\n".join(lines[-n:])


def session_json(store: Store, s: Session) -> dict:
    cls, text = status_of(store, s)
    frac = store.ctx_frac(s)
    return {
        "key": s.key, "host": s.host, "kind": s.kind, "agent": s.agent,
        "title": s.title or s.name or "(untitled)", "name": s.name, "cwd": s.short_cwd,
        "status": cls, "status_text": text, "model": s.model_label,
        "ctx": round(frac, 3) if frac is not None else None, "updated": s.updated,
        "git": f"{s.git['repo']} ⎇ {s.git['branch']}" if s.git else "",
        "pinned": s.ident in store.state["pinned"],
        "screen": screen_tail(s.screen) if s.kind == "tmux" else "",
        "last_user": s.last_user[:600], "last_assistant": s.last_assistant[-1200:],
        "snippet": s.snippet, "attached": s.attached, "tty": s.tty,
    }


def state_json(app) -> dict:
    st: Store = app["store"]
    return {
        "now": time.time(),
        "hosts": [{"name": h, "state": st.host_state.get(h, "connecting")} for h in st.hosts],
        "live": [session_json(st, s) for s in st.live()],
        "recent": [session_json(st, s) for s in st.recent()[:60]],
        "usage": st.codex_usage(),
        "dirs": {h: st.recent_dirs(h) for h in st.hosts},
    }


def find(app, key: str) -> Session | None:
    st: Store = app["store"]
    for s in st.live(show_background=True):
        if s.key == key:
            return s
    for h in st.hosts:
        for s in st.history[h] + st.search.get(h, []):
            if s.key == key:
                return s
    return None


# ---------------------------------------------------------------- http

@web.middleware
async def auth(request, handler):
    if request.path.startswith(("/api/", "/ws/")) and request.path != "/api/pair":
        tok = (request.headers.get("X-Termwall-Token") or request.query.get("token")
               or request.cookies.get("tw") or "")
        if not secrets.compare_digest(tok, request.app["token"]):
            return web.json_response({"error": "not paired"}, status=401)
    resp = await handler(request)
    if not request.path.startswith(("/api/", "/ws/")):
        # always revalidate (cheap 304s): a stale app.js on the phone means fixes never land
        resp.headers["Cache-Control"] = "no-cache"
    return resp


async def api_pair(request):
    """Exchange a short-lived 6-digit code (from `termwall web-pair`) for the token.
    iOS home-screen apps don't share storage with the browser that installed them,
    so the app pairs itself. The code dies after 10 minutes or 5 wrong tries."""
    path = request.app.get("pair_path", PAIR_PATH)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return web.json_response({"ok": False, "error": "no pairing code active: run termwall web-pair"}, status=403)
    body = await request.json()
    code = str(body.get("code", "")).strip()
    if time.time() > data.get("expires", 0) or data.get("tries", 0) >= PAIR_TRIES:
        path.unlink(missing_ok=True)
        return web.json_response({"ok": False, "error": "code expired: run termwall web-pair again"}, status=403)
    if not secrets.compare_digest(code, data["code"]):
        data["tries"] = data.get("tries", 0) + 1
        path.write_text(json.dumps(data))
        await asyncio.sleep(1)   # slow down guessing
        return web.json_response({"ok": False, "error": "wrong code"}, status=403)
    path.unlink(missing_ok=True)
    return web.json_response({"ok": True, "token": request.app["token"]})


async def api_clientlog(request):
    """The phone app reports its own errors here (shows up in the service log)."""
    body = await request.json()
    print("client:", json.dumps(body)[:2000], file=sys.stderr, flush=True)
    return web.json_response({"ok": True})


async def api_state(request):
    request.app["store"].reload_state()
    return web.json_response(state_json(request.app))


async def ws_state(request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    app = request.app
    app["state_clients"].add(ws)
    try:
        await ws.send_json(state_json(app))
        async for _ in ws:
            pass
    finally:
        app["state_clients"].discard(ws)
    return ws


async def broadcaster(app):
    """Push state to every open app on change (coalesced)."""
    while True:
        await app["changed"].wait()
        await asyncio.sleep(0.25)
        app["changed"].clear()
        app["store"].reload_state()
        if app["state_clients"]:
            payload = json.dumps(state_json(app))
            for ws in list(app["state_clients"]):
                try:
                    await ws.send_str(payload)
                except Exception:
                    app["state_clients"].discard(ws)
        events, app["events"] = app["events"], []
        for kind, s in events:
            await app["push"].notify(kind, s)


def need(app, body, *keys):
    s = find(app, body.get("key", ""))
    if not s:
        raise web.HTTPNotFound(text="no such session")
    return s, app["hosts"][s.host]


async def api_reply(request):
    body = await request.json()
    s, h = need(request.app, body)
    if s.kind != "tmux":
        return web.json_response({"ok": False, "error": "not a tmux session"})
    return web.json_response(await h.call("send", target=s.tmux_id, text=body.get("text", ""),
                                          enter=body.get("enter", True)))


async def api_keys(request):
    """Raw tmux key names (Escape, BTab, C-c, Up...) for the app's key bar."""
    body = await request.json()
    s, h = need(request.app, body)
    keys = [k for k in body.get("keys", [])[:8] if re.fullmatch(r"[A-Za-z0-9-]{1,12}", k)]
    return web.json_response(await h.call("keys", target=s.tmux_id, keys=keys))


async def api_pref(request):
    """Pin / hide from the phone; shared with the desktop wall via the prefs host."""
    body = await request.json()
    s, _ = need(request.app, body)
    which = body.get("which")
    if which not in ("pinned", "hidden"):
        raise web.HTTPBadRequest(text="which = pinned | hidden")
    on = request.app["store"].toggle(which, s)
    request.app["changed"].set()
    return web.json_response({"ok": True, "on": on})


async def api_kill(request):
    body = await request.json()
    s, h = need(request.app, body)
    return web.json_response(await h.call("kill", target=s.tmux_id))


async def api_rename(request):
    body = await request.json()
    s, h = need(request.app, body)
    return web.json_response(await h.call("rename", target=s.tmux_id, name=body["name"]))


async def spawn(app, host, name, cwd, command):
    h = app["hosts"][host]
    r = await h.call("new", name=name, cwd=cwd, command=command)
    if r.get("ok"):
        r["key"] = f"{host}/tmux/{r['session_id']}"
        await asyncio.sleep(0.6)  # let the next snapshot include it
    return r


def slug(text: str, n=4) -> str:
    return "-".join(re.findall(r"[A-Za-z0-9]+", text.lower())[:n]) or "session"


def live_twin(store: Store, s: Session) -> Session | None:
    """Already running? (same conversation on the same host) -> open that instead."""
    for x in store.live(s.host, show_background=True):
        if x.kind == "tmux" and s.resume_id and x.resume_id == s.resume_id:
            return x
    return None


async def api_resume(request):
    body = await request.json()
    s, _ = need(request.app, body)
    twin = live_twin(request.app["store"], s)
    if twin:
        return web.json_response({"ok": True, "key": twin.key, "name": twin.name, "existing": True})
    cfg = request.app["cfg"]
    tmpl = cfg.agents.get(f"{s.agent}_resume", "")
    return web.json_response(await spawn(request.app, s.host, slug(s.title, 4), s.cwd,
                                         tmpl.format(id=s.resume_id)))


async def api_new(request):
    body = await request.json()
    cfg = request.app["cfg"]
    agent = body.get("agent", "claude")
    cmd = None if agent == "shell" else cfg.agents.get(agent, agent)
    cwd = body.get("cwd") or "~"
    name = body.get("name") or os.path.basename(cwd.rstrip("/")) or agent
    if name == "~":
        name = agent
    if body.get("host") not in request.app["hosts"]:
        raise web.HTTPBadRequest(text="unknown host")
    return web.json_response(await spawn(request.app, body["host"], name, cwd, cmd))


async def api_peek(request):
    s = find(request.app, request.query.get("key", ""))
    if not s:
        raise web.HTTPNotFound()
    h = request.app["hosts"][s.host]
    if s.kind == "tmux":
        r = await h.call("capture", target=s.tmux_id, lines=2000)
        return web.json_response({"kind": "screen", "text": r.get("screen", "")})
    if s.resume_id:
        r = await h.call("transcript", kind=s.agent, id=s.resume_id, limit=80, timeout=60)
        return web.json_response({"kind": "chat", "messages": r.get("messages", [])})
    return web.json_response({"kind": "none"})


async def api_search(request):
    q = request.query.get("q", "").strip()
    st: Store = request.app["store"]
    st.search_q = q if len(q) >= 3 else ""
    st.search = {}
    if not st.search_q:
        return web.json_response({"live": [], "recent": []})
    skip = [x.replace("/", "-").replace(".", "-") for x in request.app["cfg"].ui.get("hide_cwd", [])]
    results = await asyncio.gather(*[h.call("search", q=q, skip=skip, timeout=60)
                                     for h in request.app["hosts"].values()])
    for name, r in zip(request.app["hosts"], results):
        st.set_search(name, st.search_q, r.get("results") or [])
    live = [session_json(st, s) for s in st.live() if s.snippet]
    recent = [session_json(st, s) for s in st.recent() if s.snippet]
    return web.json_response({"live": live, "recent": recent})


# ---------------------------------------------------------------- terminal bridge

def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def ws_term(request):
    """xterm.js <-> pty running `tmux attach` (via ssh for remote hosts).
    Closing the socket only detaches; the session keeps running."""
    s = find(request.app, request.query.get("key", ""))
    if not s or s.kind != "tmux":
        raise web.HTTPNotFound()
    h: Host = request.app["hosts"][s.host]
    cols = int(request.query.get("cols", 80))
    rows = int(request.query.get("rows", 24))
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    pid, fd = pty.fork()
    if pid == 0:  # child
        argv = h.attach_argv(s.tmux_id)
        os.execvpe(argv[0], argv, clean_env(TERM="xterm-256color", LANG=os.environ.get("LANG", "C.UTF-8")))
    set_winsize(fd, rows, cols)
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()

    def readable():
        try:
            data = os.read(fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        out_q.put_nowait(data)
        if not data:
            loop.remove_reader(fd)

    loop.add_reader(fd, readable)

    async def pump_out():
        while True:
            data = await out_q.get()
            if not data:
                await ws.close()
                return
            # coalesce bursts into one frame
            while not out_q.empty():
                more = out_q.get_nowait()
                if not more:
                    out_q.put_nowait(more)
                    break
                data += more
            await ws.send_bytes(data)

    pump = asyncio.create_task(pump_out())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") == "i":
                    os.write(fd, m["d"].encode())
                elif m.get("t") == "r":
                    set_winsize(fd, max(5, int(m["r"])), max(20, int(m["c"])))
            elif msg.type == WSMsgType.BINARY:
                os.write(fd, msg.data)
    finally:
        pump.cancel()
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGHUP)  # detach: tmux client exits, session stays
            os.close(fd)
            await asyncio.sleep(0.2)
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
    return ws


# ---------------------------------------------------------------- push (filled in by webpush.py)

class NoPush:
    async def notify(self, kind, s):
        pass


# ---------------------------------------------------------------- app

async def static(request):
    name = request.match_info.get("name") or "index.html"
    for base in (WEB_DIR, WEB_DIR / "vendor", WEB_DIR / "icons"):
        p = (base / name).resolve()
        if p.is_file() and base.resolve() in p.parents:
            return web.FileResponse(p)
    raise web.HTTPNotFound()


def make_app() -> web.Application:
    cfg = config.load()
    app = web.Application(middlewares=[auth])
    app["cfg"] = cfg
    app["token"] = token()
    store = WebStore(cfg)
    app["store"] = store
    app["changed"] = asyncio.Event()
    app["events"] = []
    app["state_clients"] = set()
    app["hosts"] = {h.name: Host(app, h) for h in cfg.hosts}

    def send_prefs(add, remove):
        h = app["hosts"].get(store.prefs_host)
        if h:
            asyncio.get_event_loop().create_task(h.call("prefs_set", add=add, remove=remove))
    store.on_prefs_change = send_prefs
    try:
        from .webpush import Push
        app["push"] = Push(app)
    except Exception as e:  # push is optional
        print("web push disabled:", e, file=sys.stderr)
        app["push"] = NoPush()
    r = app.router
    r.add_post("/api/pair", api_pair)
    r.add_post("/api/clientlog", api_clientlog)
    r.add_get("/api/state", api_state)
    r.add_get("/ws/state", ws_state)
    r.add_get("/ws/term", ws_term)
    r.add_post("/api/reply", api_reply)
    r.add_post("/api/keys", api_keys)
    r.add_post("/api/kill", api_kill)
    r.add_post("/api/pref", api_pref)
    r.add_post("/api/rename", api_rename)
    r.add_post("/api/resume", api_resume)
    r.add_post("/api/new", api_new)
    r.add_get("/api/peek", api_peek)
    r.add_get("/api/search", api_search)
    if hasattr(app["push"], "routes"):
        app["push"].routes(r)
    r.add_get("/", static)
    r.add_get("/{name:[A-Za-z0-9_.-]+}", static)

    app["tasks"] = []

    async def start(app):
        for h in app["hosts"].values():
            app["tasks"].append(asyncio.create_task(h.run()))
        app["tasks"].append(asyncio.create_task(broadcaster(app)))

    async def stop(app):
        for t in app["tasks"]:
            t.cancel()
        for h in app["hosts"].values():
            if h.proc and h.proc.returncode is None:
                h.proc.kill()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


from aiohttp.abc import AbstractAccessLogger  # noqa: E402


class PathOnlyLog(AbstractAccessLogger):
    """status, method, path, ms. Never the query string (websocket URLs carry the token)."""

    def log(self, request, response, time_):
        self.logger.info("%s %s %s %.0fms", response.status, request.method, request.path, time_ * 1000)


def run():
    import logging
    for k in [k for k in os.environ if leaked_agent_var(k)]:
        del os.environ[k]
    # request log (no query strings: the websocket URLs carry the token)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    cfg = config.load()
    w = cfg.web
    web.run_app(make_app(), host=w.get("bind", "127.0.0.1"), port=int(w.get("port", 8770)),
                print=lambda *a: print(*a, flush=True), access_log_class=PathOnlyLog)
