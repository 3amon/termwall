"""~/.config/termwall/config.toml loading (written with defaults on first run)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "termwall"
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_PATH = CONFIG_DIR / "state.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/termwall-{os.getuid()}")
SOCKET_PATH = RUNTIME_DIR / "termwall.sock"
SSH_DIR = Path.home() / ".cache" / "termwall"

DEFAULT_CONFIG = """\
# termwall config. Restart the daemon after editing:  termwall restart

# Every machine whose sessions should show up. `ssh` is the ssh destination;
# omit it for the machine termwall runs on. Hosts are shown in this order.
[[hosts]]
name = "workstation"

[[hosts]]
name = "buildbox"
ssh = "buildbox"          # user@host, or a Host alias from ~/.ssh/config

# [[hosts]]
# name = "laptop"
# ssh = "laptop"

[agents]
# Commands typed into a fresh tmux session. {id} is the session id to resume.
claude = "claude"
claude_resume = "claude -r {id}"
codex = "codex"
codex_resume = "codex resume {id}"
# Claude doesn't log its context window; the card's context meter uses this
claude_context = 1000000

[terminal]
# "kitty" = one dedicated kitty window, one tab per session (fast switching).
# "command" = run `command` with {cmd} replaced by the attach command.
kind = "kitty"
# command = "gnome-terminal -- {cmd}"
kitty_tab_bar = true

[notify]
# desktop notifications (master switch: notify under [ui])
approvals = true          # an agent is blocked on a permission / confirm / password prompt
finished = "always"       # an agent finished its turn: "always" | "away" | "never"
quiet_hours = ""          # e.g. "23:00-08:00": no desktop notifications in this window
away_minutes = 5          # idle this long, or screen locked, counts as away
# phone push via ntfy (https://ntfy.sh): install the ntfy app and subscribe to the topic.
# Only host + session name are sent.
phone = "approvals"       # pushed while you're away: "approvals" | "all" | "never"
ntfy_server = "https://ntfy.sh"
# "app" once the phone app (termwall web, possibly on another host) does push; stops ntfy here
phone_via = "ntfy"
# the machine you sit at: phone pushes go out when *it* is locked / idle
desk_host = ""
ntfy_topic = ""

[web]
# phone app (termwall web). Keep it on localhost and publish with `tailscale serve`.
bind = "127.0.0.1"
port = 8770
# contact address sent to the push services (VAPID "sub" claim): mailto: or https origin
# vapid_subject = "mailto:you@example.net"

[ui]
# "mru" = most recently opened first, preselect the previous one (alt-tab feel)
# "stable" = host order, then creation time
order = "mru"
poll_seconds_shown = 1.0
poll_seconds_hidden = 3.0
# desktop notification (with an Open button) when an agent finishes its turn
notify = true
# rename tmux sessions still named after their folder to the conversation title
auto_name = true
# host that keeps the shared pins / hidden / renamed list (desktop wall + phone app agree)
prefs_host = ""
# tmux sessions idle (not attached, not working) this many days are marked stale
stale_days = 7
# history entries whose cwd contains one of these are hidden (background bots)
hide_cwd = []            # e.g. [".bots/"]
"""


@dataclass
class Host:
    name: str
    ssh: str | None = None


@dataclass
class Config:
    hosts: list[Host]
    agents: dict = field(default_factory=dict)
    terminal: dict = field(default_factory=dict)
    ui: dict = field(default_factory=dict)
    notify: dict = field(default_factory=dict)
    web: dict = field(default_factory=dict)


def load() -> Config:
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
    defaults = tomllib.loads(DEFAULT_CONFIG)
    raw = tomllib.loads(CONFIG_PATH.read_text())
    hosts = [Host(h["name"], h.get("ssh")) for h in raw.get("hosts", defaults["hosts"])]
    merged = {k: {**defaults[k], **raw.get(k, {})} for k in ("agents", "terminal", "ui", "notify", "web")}
    return Config(hosts=hosts, **merged)


def set_option(section: str, key: str, value) -> None:
    """Change one `key = value` in config.toml in place, keeping comments."""
    import json as _json
    import re as _re
    lit = ("true" if value else "false") if isinstance(value, bool) else (
        str(value) if isinstance(value, (int, float)) else _json.dumps(value))
    text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else DEFAULT_CONFIG
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
    if start is None:
        lines += ["", f"[{section}]", f"{key} = {lit}"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        for i in range(start + 1, end):
            if _re.match(rf"\s*{_re.escape(key)}\s*=", lines[i]):
                lines[i] = f"{key} = {lit}"
                break
        else:
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, f"{key} = {lit}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
