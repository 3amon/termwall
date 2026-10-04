"""One long-lived agent process per host (local subprocess or ssh), as QProcess."""
from __future__ import annotations

import base64
import itertools
import json
import shlex
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .config import SSH_DIR, Host

AGENT_SRC = (Path(__file__).parent / "agent.py").read_bytes()


def ssh_base(dest: str) -> list[str]:
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
        "-o", "ControlMaster=auto", "-o", f"ControlPath={SSH_DIR}/ssh-%C",
        "-o", "ControlPersist=30m", dest,
    ]


def agent_bootstrap(interval: float) -> str:
    b64 = base64.b64encode(AGENT_SRC).decode()
    code = f"import base64;exec(compile(base64.b64decode('{b64}'),'termwall-agent','exec'))"
    return f"python3 -u -c {shlex.quote(code)} --stream {interval}"


class HostConn(QObject):
    snapshot = Signal(str, dict)     # host name, snapshot
    status = Signal(str, str)        # host name, "connecting" | "online" | "offline: why"

    def __init__(self, host: Host, interval: float, parent=None):
        super().__init__(parent)
        self.host = host
        self.interval = interval
        self.proc: QProcess | None = None
        self.buf = b""
        self.err = b""
        self.state = "connecting"
        self.last_seen = 0.0
        self.info: dict = {}         # platform, tmux path, home from last snapshot
        self.pending: dict[int, callable] = {}
        self.rids = itertools.count(1)
        self.backoff = 1.0
        self.retry = QTimer(self, singleShot=True, timeout=self.start)

    # ---------------------------------------------------------------- process
    def start(self):
        self.buf = self.err = b""
        p = QProcess(self)
        p.readyReadStandardOutput.connect(self._read)
        p.readyReadStandardError.connect(lambda: setattr(self, "err", (self.err + p.readAllStandardError().data())[-2000:]))
        p.finished.connect(self._finished)
        if self.host.ssh:
            remote = "PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; " + agent_bootstrap(self.interval)
            args = ssh_base(self.host.ssh) + ["-T", remote]
        else:
            args = [sys.executable, "-u", "-c",
                    f"import base64;exec(compile(base64.b64decode('{base64.b64encode(AGENT_SRC).decode()}'),'termwall-agent','exec'))",
                    "--stream", str(self.interval)]
        self.proc = p
        self._set_state("connecting")
        p.start(args[0], args[1:])

    def stop(self):
        self.retry.stop()
        if self.proc:
            self.proc.finished.disconnect()
            self.proc.kill()
            self.proc.waitForFinished(1000)
            self.proc = None

    def _finished(self, code, _status):
        why = self.err.decode(errors="replace").strip().splitlines()
        why = why[-1] if why else f"exit {code}"
        self._set_state(f"offline: {why}")
        for cb in self.pending.values():
            cb({"ok": False, "error": "host disconnected"})
        self.pending.clear()
        self.retry.start(int(self.backoff * 1000))
        self.backoff = min(self.backoff * 2, 30)

    def _set_state(self, s):
        self.state = s
        self.status.emit(self.host.name, s)

    def _read(self):
        self.buf += self.proc.readAllStandardOutput().data()
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "snapshot":
                self.last_seen = time.time()
                self.backoff = 1.0
                self.info = {k: msg.get(k) for k in ("platform", "tmux", "home", "host")}
                if self.state != "online":
                    self._set_state("online")
                self.snapshot.emit(self.host.name, msg)
            elif msg.get("type") == "reply":
                cb = self.pending.pop(msg.get("rid"), None)
                if cb:
                    cb(msg)

    # ---------------------------------------------------------------- commands
    def send(self, op: str, callback=None, **kw):
        if not self.proc or self.proc.state() != QProcess.Running:
            if callback:
                callback({"ok": False, "error": f"{self.host.name} is offline"})
            return
        rid = next(self.rids)
        if callback:
            self.pending[rid] = callback
        self.proc.write((json.dumps({"op": op, "rid": rid, **kw}) + "\n").encode())

    def set_interval(self, seconds: float):
        self.interval = seconds
        self.send("interval", seconds=seconds)

    # ---------------------------------------------------------------- attach
    def attach_argv(self, tmux_target: str) -> list[str]:
        """argv that attaches a terminal to a tmux session on this host."""
        tm = self.info.get("tmux") or "tmux"
        if not self.host.ssh:
            return [tm, "attach-session", "-t", tmux_target]
        remote = f"exec {shlex.quote(tm)} attach-session -t {shlex.quote(tmux_target)}"
        return ssh_base(self.host.ssh) + ["-t", remote]
