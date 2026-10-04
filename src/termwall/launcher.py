"""Put a session in front of the user in a native terminal.

kitty mode keeps ONE dedicated kitty OS window (its own remote-control socket,
separate from the user's everyday kitty) with one tab per session. Opening a
session that already has a tab just focuses that tab, so zipping between
sessions is a tab switch, not a new window.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from .config import RUNTIME_DIR

KITTY_SOCK = RUNTIME_DIR / "termwall-kitty.sock"
KITTY_CLASS = "termwall-term"


def _kitty() -> str:
    return shutil.which("kitty") or "kitty"


def _kitten_at(*args, timeout=3) -> subprocess.CompletedProcess:
    return subprocess.run([_kitty(), "@", "--to", f"unix:{KITTY_SOCK}", *args],
                          capture_output=True, text=True, timeout=timeout)


class Launcher:
    def __init__(self, cfg):
        self.cfg = cfg.terminal
        self.kind = self.cfg.get("kind", "kitty")
        if self.kind == "kitty" and not shutil.which("kitty"):
            self.kind = "macos" if sys.platform == "darwin" else "command"

    def open(self, key: str, title: str, argv: list[str], remote: bool,
             screen: tuple[int, int, int, int] | None = None) -> str | None:
        """Show session `key` (on `screen` if given); returns an error string or None."""
        try:
            if self.kind == "kitty":
                err = self._kitty(key, title, argv, remote)
                if not err and sys.platform.startswith("linux"):
                    from . import x11
                    x11.present(KITTY_CLASS, screen)
                return err
            if self.kind == "macos":
                return self._macos(argv, remote)
            return self._command(argv, remote)
        except Exception as e:  # never take the daemon down
            return repr(e)

    # ---------------------------------------------------------------- kitty
    def _kitty_windows(self):
        if not KITTY_SOCK.exists():
            return None
        try:
            r = _kitten_at("ls", timeout=2)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)

    def _kitty(self, key, title, argv, remote):
        env = []
        if remote:
            # kitty forces TERM=xterm-kitty, which macOS/most remotes lack terminfo for
            argv = ["env", "TERM=xterm-256color", *argv]
        tree = self._kitty_windows()
        if tree is not None:
            for osw in tree:
                for tab in osw["tabs"]:
                    for w in tab["windows"]:
                        if w.get("user_vars", {}).get("termwall_key") == key:
                            _kitten_at("focus-window", "--match", f"id:{w['id']}")
                            return None
            r = _kitten_at("launch", "--type=tab", "--tab-title", title, "--title", title,
                           "--var", f"termwall_key={key}", *env, "--", *argv)
            if r.returncode != 0:
                return r.stderr.strip() or "kitty launch failed"
            _kitten_at("focus-window", "--match", f"var:termwall_key={key}")
            return None
        # no termwall kitty yet: start one whose first tab is this session
        KITTY_SOCK.unlink(missing_ok=True)
        sess = tempfile.NamedTemporaryFile("w", prefix="termwall-", suffix=".kitty", delete=False)
        sess.write(f"new_tab {title}\n")
        sess.write("launch " + " ".join(shlex.quote(a) for a in
                   ["--title", title, "--var", f"termwall_key={key}", *env, *argv]) + "\n")
        sess.close()
        opts = ["-o", "allow_remote_control=socket-only", "--listen-on", f"unix:{KITTY_SOCK}",
                "--class", KITTY_CLASS, "--title", "termwall",
                "-o", "confirm_os_window_close=0", "--session", sess.name]
        if not self.cfg.get("kitty_tab_bar", True):
            opts += ["-o", "tab_bar_style=hidden"]
        else:
            opts += ["-o", "tab_bar_min_tabs=1", "-o", "tab_bar_style=powerline",
                     "-o", "tab_title_template={index}: {title}"]
        for n in range(1, 10):
            opts += ["-o", f"map ctrl+{n} goto_tab {n}"]
        subprocess.Popen([_kitty(), *opts], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None

    # ---------------------------------------------------------------- others
    def _command(self, argv, remote):
        cmd = shlex.join(argv)
        if remote:
            cmd = "TERM=xterm-256color " + cmd
        tmpl = self.cfg.get("command") or "x-terminal-emulator -e {cmd}"
        subprocess.Popen(tmpl.replace("{cmd}", shlex.quote(f"sh -c {shlex.quote(cmd)}")),
                         shell=True, start_new_session=True)
        return None

    def _macos(self, argv, remote):
        cmd = shlex.join(argv)
        if remote:
            cmd = "TERM=xterm-256color " + cmd
        script = (f'tell application "Terminal"\n do script {json.dumps(cmd)}\n activate\nend tell')
        subprocess.Popen(["osascript", "-e", script])
        return None


def focused_key() -> str | None:
    """termwall_key of the kitty tab you're looking at, if its OS window has focus."""
    try:
        r = _kitten_at("ls", timeout=2)
        if r.returncode != 0:
            return None
        for osw in json.loads(r.stdout):
            if not osw.get("is_focused"):
                continue
            for tab in osw["tabs"]:
                if tab.get("is_active"):
                    for w in tab["windows"]:
                        return w.get("user_vars", {}).get("termwall_key")
    except Exception:
        pass
    return None
