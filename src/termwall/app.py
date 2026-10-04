"""The daemon: host connections + store + overlay + control socket."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from . import config
from .hosts import HostConn
from .launcher import Launcher
from .model import Session, Store
from .overlay import Overlay, slug


class Bridge(QObject):
    error = Signal(str)
    open_key = Signal(str)


class Controller(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = config.load()
        self.store = Store(self.cfg)
        self.launcher = Launcher(self.cfg)
        self.overlay = Overlay(self.store)
        from .tray import Tray, app_icon
        app.setWindowIcon(app_icon())
        self.bridge = Bridge()
        self.bridge.error.connect(lambda m: self.overlay.toast(m))
        self.bridge.open_key.connect(self.open_key)
        self.conns: dict[str, HostConn] = {}
        ui = self.cfg.ui
        self.poll_shown = float(ui.get("poll_seconds_shown", 1.0))
        self.poll_hidden = float(ui.get("poll_seconds_hidden", 3.0))
        for h in self.cfg.hosts:
            c = HostConn(h, self.poll_hidden, self)
            c.snapshot.connect(self.on_snapshot)
            c.status.connect(self.on_status)
            self.conns[h.name] = c
            c.start()
        self.dirty = False
        self.coalesce = QTimer(self, singleShot=True, interval=30, timeout=self._flush)
        self.store.on_prefs_change = self.send_prefs
        o = self.overlay
        o.open_requested.connect(self.open_session)
        o.new_requested.connect(self.new_session)
        o.kill_requested.connect(lambda s: self.conns[s.host].send("kill", target=s.tmux_id))
        o.rename_requested.connect(self.rename)
        o.reply_requested.connect(self.reply)
        o.search_requested.connect(self.search)
        o.peek_requested.connect(self.fetch_peek)
        o.visibility_changed.connect(self.on_visibility)
        self.server = QLocalServer(self)
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        QLocalServer.removeServer(str(config.SOCKET_PATH))
        if not self.server.listen(str(config.SOCKET_PATH)):
            print("termwall: cannot listen on", config.SOCKET_PATH, self.server.errorString(), file=sys.stderr)
        self.server.newConnection.connect(self.on_client)
        self.tray = Tray(self) if self.cfg.ui.get("tray", True) else None

    # ---------------------------------------------------------------- hosts
    def on_snapshot(self, host, snap):
        self.store.ingest(host, snap)
        restore = self.store.track(host, snap)
        if restore:
            self.restore(host, restore)
        finished, self.store.finished = self.store.finished, []
        for s in finished:
            self.notify(s)
        attention, self.store.attention = self.store.attention, []
        for s in attention:
            self.notify(s, approval=True)
        self.auto_name(host)
        self.dirty = True
        if not self.coalesce.isActive():
            self.coalesce.start()

    def _flush(self):
        if self.dirty:
            self.dirty = False
            self.overlay.on_store_changed()
            if self.tray:
                self.tray.refresh()

    def on_status(self, host, state):
        self.store.host_state[host] = state
        self.overlay.update()
        if getattr(self, "tray", None):
            self.tray.refresh()

    def on_visibility(self, shown):
        for c in self.conns.values():
            c.set_interval(self.poll_shown if shown else self.poll_hidden)

    # ---------------------------------------------------------------- actions
    def restore(self, host, specs):
        """Host rebooted: bring its tmux sessions back (agents resumed by id)."""
        conn = self.conns[host]
        names = []
        for spec in specs:
            agent, rid = spec.get("agent"), spec.get("resume_id")
            if agent in ("claude", "codex"):
                tmpl = self.cfg.agents.get(f"{agent}_resume" if rid else agent, agent)
                cmd = tmpl.format(id=rid)
            else:
                cmd = None
            conn.send("new", name=spec["name"], cwd=spec.get("cwd") or "~", command=cmd)
            names.append(spec["name"])
        msg = f"{host} rebooted — restored {len(names)} session(s): {', '.join(names)}"
        self.overlay.toast(msg, 8)
        if sys.platform.startswith("linux"):
            subprocess.Popen(["notify-send", "-a", "termwall", "-i", "termwall", "termwall", msg])

    # ---------------------------------------------------------------- notifications
    def notify(self, s: Session, approval=False):
        """Agent finished its turn / is blocked on a prompt: desktop notification with an
        Open button, and a phone push (ntfy) when you're away from the machine."""
        n, ui = self.cfg.notify, self.cfg.ui
        title = f"{s.host} \u00b7 {s.title or s.name or s.agent}"
        if approval:
            from .model import ANSI_RE, APPROVAL_RE
            title = "\u26a0 needs approval \u2014 " + title
            lines = [ln.strip() for ln in ANSI_RE.sub("", s.screen).splitlines() if ln.strip()]
            hits = [ln for ln in lines[-8:] if APPROVAL_RE.search(ln)]
            body = (hits[0] if hits else "waiting on a prompt")[:180]
        else:
            body = (s.last_assistant or "finished").strip().splitlines()
            body = (body[-1] if body else "")[:180]
        overlay_up = self.overlay.isVisible()

        def work():
            from . import presence
            from .launcher import focused_key
            is_away = presence.away(float(n.get("away_minutes", 5)))
            # phone: only while away; minimal payload (it leaves the machine)
            phone = n.get("phone", "approvals")
            from .webpush_state import app_push_enabled
            if (is_away and n.get("ntfy_topic") and not app_push_enabled()
                    and (phone == "all" or (phone == "approvals" and approval))):
                err = presence.push(n.get("ntfy_server", "https://ntfy.sh"), n["ntfy_topic"],
                                    "termwall: " + ("needs approval" if approval else "finished"),
                                    f"{s.host} \u00b7 {s.name or s.agent}", approval)
                if err:
                    print("ntfy push failed:", err, file=sys.stderr)
            # desktop
            if not ui.get("notify", True) or overlay_up or presence.in_quiet_hours(n.get("quiet_hours", "")):
                return
            if approval and not n.get("approvals", True):
                return
            if not approval:
                mode = n.get("finished", "always")
                if mode == "never" or (mode == "away" and not is_away):
                    return
            if focused_key() == s.key:
                return  # you're already looking at it
            if sys.platform == "darwin":
                subprocess.run(["osascript", "-e", f"display notification {json.dumps(body)} "
                                f"with title {json.dumps(title)}"])
                return
            r = subprocess.run(["notify-send", "-a", "termwall", "-i", "termwall",
                                "-u", "critical" if approval else "normal",
                                "-t", "20000",
                                # "default" = clicking the notification itself (GNOME), "open" = the button
                                "-A", "default=Open", "-A", "open=Open", title, body],
                               capture_output=True, text=True)
            if r.stdout.strip() in ("open", "default"):
                self.bridge.open_key.emit(s.key)
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- session ops
    def send_prefs(self, add, remove):
        conn = self.conns.get(self.store.prefs_host)
        if conn:
            conn.send("prefs_set", add=add, remove=remove)

    def rename(self, s: Session, name: str):
        if s.key not in self.store.state["named"]:
            self.store.state["named"].append(s.key)   # yours now: no auto-naming
            self.store.save()
            self.store.push_pref({"named": [s.key]}, {})
        self.conns[s.host].send("rename", target=s.tmux_id, name=name,
                                callback=lambda r: None if r.get("ok")
                                else self.overlay.toast(r.get("error") or "rename failed"))

    def reply(self, s: Session, text: str):
        self.store.state["seen"][s.key] = __import__("time").time()
        self.conns[s.host].send("send", target=s.tmux_id, text=text, enter=True,
                                callback=lambda r: None if r.get("ok")
                                else self.overlay.toast(r.get("error") or "send failed"))

    def search(self, q: str):
        self.store.search = {}
        if len(q) < 3:
            self.overlay.on_store_changed()
            return
        skip = [x.replace("/", "-").replace(".", "-") for x in self.cfg.ui.get("hide_cwd", [])]
        for host, conn in self.conns.items():
            def done(r, host=host):
                self.store.set_search(host, q, r.get("results") or [])
                self.overlay.on_store_changed()
            conn.send("search", callback=done, q=q, skip=skip)

    def fetch_peek(self, s: Session):
        conn = self.conns.get(s.host)
        if not conn:
            return
        if s.kind == "tmux":
            conn.send("capture", target=s.tmux_id, lines=3000, callback=lambda r: r.get("ok") and
                      self.overlay.set_peek_content(s.key, "screen", r["screen"]))
        elif s.resume_id:
            conn.send("transcript", kind=s.agent, id=s.resume_id, limit=80, callback=lambda r: r.get("ok") and
                      self.overlay.set_peek_content(s.key, "chat", r["messages"]))

    def auto_name(self, host):
        """Rename tmux sessions still called after their directory (claude, claude-2,
        termwall...) to the conversation's title, once. Sessions you renamed are left alone."""
        import re as _re
        from .overlay import slug
        if not self.cfg.ui.get("auto_name", True):
            return
        conn = self.conns[host]
        done = self.store.state.setdefault("auto_named", {})
        sessions = [x for x in self.store.by_host.get(host, {}).values() if x.kind == "tmux"]
        taken = {x.name for x in sessions}
        for x in sessions:
            if (not x.title or x.agent not in ("claude", "codex") or not x.has_title
                    or x.key in done or x.key in self.store.state["named"]):
                continue
            base = _re.sub(r"[^A-Za-z0-9_-]", "-", __import__("os").path.basename((x.cwd or "").rstrip("/")))
            generic = {base, x.agent, "claude", "codex", "session", "shell"}
            stem = _re.sub(r"-\d+$", "", x.name)
            if stem not in generic:
                done[x.key] = x.name   # already meaningful
                continue
            new = slug(x.title, 4)[:32].strip("-") or x.name
            cand, i = new, 2
            while cand in taken:
                cand, i = f"{new}-{i}", i + 1
            taken.add(cand)
            done[x.key] = cand
            conn.send("rename", target=x.tmux_id, name=cand)
        self.store.save()

    def open_key(self, key):
        for s in self.store.live():
            if s.key == key and s.kind == "tmux":
                return self.open_session(s, screen=None)

    def _launch(self, conn: HostConn, key: str, title: str, tmux_id: str, screen=None):
        argv = conn.attach_argv(tmux_id)
        if screen is None:
            from PySide6.QtGui import QCursor, QGuiApplication
            scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            g = scr.geometry()
            screen = (g.x(), g.y(), g.width(), g.height())

        def work():
            err = self.launcher.open(key, title, argv, remote=bool(conn.host.ssh), screen=screen)
            if err:
                self.bridge.error.emit(f"terminal: {err}")
        threading.Thread(target=work, daemon=True).start()

    def overlay_screen(self):
        if self.overlay.isVisible():
            g = self.overlay.geometry()
            return (g.x(), g.y(), g.width(), g.height())
        return None

    def open_session(self, s: Session, screen="overlay"):
        conn = self.conns[s.host]
        if screen == "overlay":
            screen = self.overlay_screen()
        if s.kind == "tmux":
            self.store.mark_opened(s)
            self._launch(conn, s.key, f"{s.host}:{s.name}", s.tmux_id, screen)
        elif s.kind == "history":
            twin = next((x for x in self.store.live(s.host, show_background=True)
                         if x.kind == "tmux" and s.resume_id and x.resume_id == s.resume_id), None)
            if twin:   # already running: open it rather than starting a second copy
                self.store.mark_opened(twin)
                self._launch(conn, twin.key, f"{twin.host}:{twin.name}", twin.tmux_id, self.overlay_screen())
                self.overlay.finish_open(True)
                return
            tmpl = self.cfg.agents.get(f"{s.agent}_resume", "")
            self._spawn(conn, name=slug(s.title), cwd=s.cwd, command=tmpl.format(id=s.resume_id))

    def new_session(self, spec):
        conn = self.conns.get(spec["host"])
        if not conn:
            return self.overlay.finish_open(False, "unknown host")
        cmd = None if spec["agent"] == "shell" else self.cfg.agents.get(spec["agent"], spec["agent"])
        name = spec["name"] or os.path.basename((spec["cwd"] or "~").rstrip("/")) or spec["agent"]
        if name == "~":
            name = spec["agent"]
        self._spawn(conn, name=name, cwd=spec["cwd"], command=cmd)

    def _spawn(self, conn: HostConn, name, cwd, command):
        self.overlay.toast(f"starting {name} on {conn.host.name}…", 2)

        def done(r):
            if not r.get("ok"):
                return self.overlay.finish_open(False, r.get("error") or "could not start session")
            key = f"{conn.host.name}/tmux/{r['session_id']}"
            self.store.state["opened"][key] = __import__("time").time()
            self.store.save()
            self._launch(conn, key, f"{conn.host.name}:{r['name']}", r["session_id"], self.overlay_screen())
            self.overlay.finish_open(True)
        conn.send("new", callback=done, name=name, cwd=cwd, command=command)

    # ---------------------------------------------------------------- control socket
    def on_client(self):
        sock = self.server.nextPendingConnection()

        def ready():
            cmd = bytes(sock.readAll().data()).decode().strip()
            reply = self.handle(cmd)
            sock.write((reply + "\n").encode())
            sock.flush()
            sock.disconnectFromServer()
        sock.readyRead.connect(ready)

    def handle(self, cmd: str) -> str:
        verb, _, arg = cmd.partition(" ")
        if verb == "toggle":
            self.overlay.toggle()
        elif verb == "show":
            if not self.overlay.isVisible():
                self.overlay.summon()
                if QApplication.platformName() == "offscreen":  # test instance: a real-sized canvas
                    self.overlay.setGeometry(0, 0, 1920, 1080)
                    self.overlay.refresh(preselect=True)
        elif verb == "hide":
            self.overlay.dismiss()
        elif verb == "debug":  # test hook
            st = self.store
            return json.dumps({"mode": self.overlay.mode, "query": self.overlay.query, "search_q": st.search_q,
                               "search": {h: [x.title for x in v] for h, v in st.search.items()},
                               "items": [x.title or x.name for x in self.overlay.items][:8]})
        elif verb == "key":
            # test hook: synthesize key presses into the overlay, e.g. "key ctrl+Return type:hi Escape"
            self.inject_keys(arg.split())
        elif verb == "quit":
            QTimer.singleShot(0, self.quit)
        elif verb == "screenshot":
            # render the overlay (even when hidden) to a PNG, for docs/tests
            o = self.overlay
            if not o.isVisible():
                o.resize(1920, 1080)
                o.refresh(preselect=True)
                o.appear_t = 1.0
            o.grab().save(arg or "termwall.png")
        elif verb == "status":
            lines = []
            for h, c in self.conns.items():
                n = len(self.store.by_host.get(h, {}))
                lines.append(f"{h}: {c.state}  live={n}  recent={len(self.store.history.get(h, []))}")
            return "\n".join(lines)
        else:
            return f"unknown command {verb!r}"
        return "ok"

    def restart(self):
        """Re-exec the daemon (picks up config + code changes)."""
        from .cli import clean_env, self_path, service_installed
        if service_installed():
            subprocess.Popen(["systemctl", "--user", "restart", "termwall"], start_new_session=True)
            return
        self.quit()
        subprocess.Popen([self_path(), "daemon"], start_new_session=True, env=clean_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def inject_keys(self, seq):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        def press(name, text=""):
            mods = Qt.NoModifier
            parts = name.split("+")
            for m in parts[:-1]:
                mods |= {"ctrl": Qt.ControlModifier, "shift": Qt.ShiftModifier, "alt": Qt.AltModifier}[m]
            last = parts[-1]
            key = getattr(Qt, "Key_" + last, None) if len(last) > 1 else None
            if key is None:
                key = Qt.Key(ord(last.upper()))
                text = text or (last if mods == Qt.NoModifier else "")
            o = self.overlay  # offscreen has no real focus: pick the open input ourselves
            target = (o.reply_edit if o.reply_edit.isVisible() else o.rename_edit if o.rename_edit.isVisible()
                      else (QApplication.focusWidget() or o) if o.new_panel.isVisible() else o)
            for t in (QEvent.KeyPress, QEvent.KeyRelease):
                QApplication.sendEvent(target, QKeyEvent(t, key, mods, text))
        for tok in seq:
            if tok.startswith("type:"):
                for ch in tok[5:].replace("_", " "):
                    press(ch if ch != " " else "Space", ch)
            else:
                press(tok)

    def quit(self):
        for c in self.conns.values():
            c.stop()
        self.server.close()
        self.app.quit()


def run(show=False):
    from .agent import leaked_agent_var
    for k in [k for k in os.environ if leaked_agent_var(k)]:
        del os.environ[k]  # kitty + local agent inherit our env
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb" if sys.platform.startswith("linux") else "")
    if not os.environ["QT_QPA_PLATFORM"]:
        del os.environ["QT_QPA_PLATFORM"]
    app = QApplication(sys.argv[:1])
    app.setApplicationName("termwall")
    app.setQuitOnLastWindowClosed(False)
    ctl = Controller(app)
    signal.signal(signal.SIGINT, lambda *_: ctl.quit())
    signal.signal(signal.SIGTERM, lambda *_: ctl.quit())
    QTimer(app, interval=300, timeout=lambda: None).start()  # let Python see signals
    if show:
        QTimer.singleShot(0, ctl.overlay.summon)
    sys.exit(app.exec())
