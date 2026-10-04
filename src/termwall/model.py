"""Merge per-host agent snapshots into a flat list of Session cards."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .config import STATE_PATH

BUSY_RE = re.compile(r"esc to interrupt|Esc to interrupt|\(esc to cancel", re.I)
# blocked on you. Agent dialogs (question + option list) sit in the last few lines;
# shell-style prompts only count on the very last line, where the cursor waits --
# otherwise an already-answered "[Y/n]" scrolled one line up would still match.
DIALOG_RE = re.compile(
    r"Do you want to (proceed|make this edit|create|run|allow|overwrite)"
    r"|Would you like to (run|make|grant|send|apply)"
    r"|Yes, (just this once|I trust this folder|and don't ask again)"
    r"|No, and tell (Codex|Claude)")
PROMPT_RE = re.compile(
    r"(\[Y/n\]|\[y/N\]|\(y/n\)|\(yes/no\)|[Pp]assword[^\n]*:|Press [Ee]nter to continue|Enter to confirm)\s*\S{0,3}\s*$")
APPROVAL_RE = re.compile(DIALOG_RE.pattern + "|" + PROMPT_RE.pattern.rstrip("$").rstrip("\\s*\\S{0,3}\\s*"))


def blocked_on_prompt(screen: str) -> bool:
    lines = [ln for ln in ANSI_RE.sub("", screen).splitlines() if ln.strip()]
    if not lines:
        return False
    return bool(DIALOG_RE.search("\n".join(lines[-6:])) or PROMPT_RE.search(lines[-1]))


ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")


@dataclass
class Session:
    key: str
    host: str
    kind: str               # "tmux" | "bare" | "history"
    agent: str              # "claude" | "codex" | "shell"
    title: str = ""
    name: str = ""          # tmux session name
    tmux_id: str = ""       # $N
    cwd: str = ""
    status: str | None = None   # "busy" | "idle" | "approval" | None
    updated: float = 0.0
    created: float = 0.0
    last_user: str = ""
    last_assistant: str = ""
    screen: str = ""
    screen_hash: str = ""
    cols: int = 80
    rows: int = 24
    attached: int = 0
    windows: int = 1
    resume_id: str = ""
    tty: str = ""
    cmd: str = ""
    background: bool = False   # sdk / bot sessions
    offline: bool = False
    home: str = ""
    model: str = ""
    effort: str = ""
    ctx: int = 0              # tokens in context after the latest reply
    ctx_window: int = 0       # 0 = unknown (Claude) -> configured size
    git: dict | None = None   # {repo, branch, common, worktree}
    snippet: str = ""         # transcript search hit
    has_title: bool = False   # agent-given title (ai-title / thread name), not just a prompt

    @property
    def ident(self) -> str:
        """Stable identity for pin/hide: survives tmux renames and reboots."""
        if self.resume_id:
            return f"{self.host}/{self.agent}/{self.resume_id}"
        if self.kind == "tmux":
            return f"{self.host}/tmux/{self.name}"
        return self.key

    @property
    def model_label(self) -> str:
        """claude-opus-5-5 -> Opus 5.5, gpt-6-astra -> GPT-6 Astra (+ effort)."""
        m = self.model
        if not m:
            return ""
        if m.startswith("claude-"):
            parts = m[len("claude-"):].split("-")
            name = parts[0].capitalize()
            nums = [x for x in parts[1:] if x.isdigit() and len(x) < 3]
            label = f"{name} {'.'.join(nums)}".strip()
        elif m.startswith("gpt-"):
            parts = m.split("-")
            label = "GPT-" + parts[1] + "".join(" " + x.capitalize() for x in parts[2:])
        else:
            label = m
        return f"{label} \u00b7 {self.effort}" if self.effort else label

    @property
    def short_cwd(self) -> str:
        c = self.cwd or ""
        if self.home and c.startswith(self.home):
            c = "~" + c[len(self.home):]
        return c

    @property
    def last_line(self) -> str:
        """The most recent text: assistant reply if any, else the prompt."""
        return self.last_assistant or self.last_user


class Store:
    """All sessions from all hosts + persisted per-session UI state (MRU, seen)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hosts = [h.name for h in cfg.hosts]
        self.by_host: dict[str, dict[str, Session]] = {h: {} for h in self.hosts}
        self.history: dict[str, list[Session]] = {h: [] for h in self.hosts}
        self.screens: dict[str, tuple[str, str]] = {}   # key -> (hash, screen)
        self.host_state: dict[str, str] = {h: "connecting" for h in self.hosts}
        try:
            self.state = json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("opened", {})   # key -> ts last opened via termwall
        self.state.setdefault("seen", {})     # key -> ts last looked at
        self.finished: list[Session] = []    # busy -> idle since last drain
        self.state.setdefault("remember", {})  # host -> {boot, sessions: name -> spec}
        self.state.setdefault("idle_at", {})  # key -> ts the agent last finished a turn
        self.state.setdefault("pinned", [])   # idents, shown first
        self.state.setdefault("hidden", [])   # idents, left off the wall
        self.state.setdefault("named", [])    # tmux keys you renamed yourself (no auto-naming)
        self.attention: list[Session] = []    # -> approval since last drain
        self.show_hidden = False
        self.codex_limits: dict[str, dict] = {}   # host -> rate_limits from its newest rollout
        self.search_q = ""
        self.search: dict[str, list[Session]] = {}
        # pins/hidden/named are shared via one host (ui.prefs_host); "" = this machine only
        self.prefs_host = cfg.ui.get("prefs_host", "") or ""
        self.on_prefs_change = None       # set by the app: fn(add: dict, remove: dict)
        self.prefs_synced = False

    def save(self):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.state))

    def mark_opened(self, s: Session):
        now = time.time()
        self.state["opened"][s.key] = now
        self.state["seen"][s.key] = now
        self.save()

    def ctx_frac(self, s: Session) -> float | None:
        window = s.ctx_window or (int(self.cfg.agents.get("claude_context", 1_000_000))
                                  if s.agent == "claude" else 0)
        return min(1.0, s.ctx / window) if (s.ctx and window) else None

    def repo_count(self, s: Session) -> int:
        if not s.git:
            return 0
        return sum(1 for x in self.by_host.get(s.host, {}).values()
                   if x.git and x.git.get("common") == s.git.get("common"))

    def toggle(self, which: str, s: Session) -> bool:
        lst = self.state[which]
        on = s.ident not in lst
        if on:
            lst.append(s.ident)
        else:
            lst.remove(s.ident)
        self.save()
        self.push_pref({which: [s.ident]} if on else {}, {} if on else {which: [s.ident]})
        return on

    def push_pref(self, add: dict, remove: dict):
        if self.prefs_host and self.on_prefs_change:
            self.on_prefs_change(add, remove)

    def apply_prefs(self, prefs: dict):
        """Adopt the shared prefs. First time: merge ours in (so nothing pinned before is lost)."""
        mine = {k: list(self.state.get(k, [])) for k in ("pinned", "hidden", "named")}
        if not self.prefs_synced:
            self.prefs_synced = True
            extra = {k: [v for v in mine[k] if v not in prefs.get(k, [])] for k in mine}
            if any(extra.values()):
                self.push_pref(extra, {})
                prefs = {k: list(prefs.get(k, [])) + extra[k] for k in mine}
        for k in mine:
            self.state[k] = list(prefs.get(k, []))
        self.save()

    def needs_you(self, s: Session) -> bool:
        """Blocked on a prompt, or an agent finished a turn since you last opened it."""
        if s.status == "approval" and not s.offline:
            return True
        if s.kind == "history" or s.agent == "shell" or s.status != "idle":
            return False
        return self.state["idle_at"].get(s.key, 0) > self.state["seen"].get(s.key, 0)

    # ---------------------------------------------------------------- ingest
    def ingest(self, host: str, snap: dict):
        home = snap.get("home") or ""
        out: dict[str, Session] = {}
        live_ids = set()
        for t in snap.get("sessions", []):
            pane = t["pane"]
            a = t.get("agent") or {}
            key = f"{host}/tmux/{t['id']}"
            if t.get("screen") is not None:
                self.screens[key] = (t["screen_hash"], t["screen"])
            screen = self.screens.get(key, ("", ""))[1]
            status = a.get("status")
            tail = "\n".join(screen.splitlines()[-8:])
            if blocked_on_prompt(screen):
                status = "approval"
            elif a and BUSY_RE.search(ANSI_RE.sub("", tail)):
                status = "busy"
            elif a and status is None:
                status = "idle"
            if a.get("id"):
                live_ids.add(a["id"])
            out[key] = Session(
                key=key, host=host, kind="tmux",
                agent=a.get("kind") or (pane["cmd"] if pane["cmd"] in ("claude", "codex") else "shell"),
                title=a.get("title") or "", name=t["name"], tmux_id=t["id"],
                cwd=a.get("cwd") or pane["cwd"], status=status,
                updated=max(t["activity"], a.get("updated") or 0), created=t["created"],
                last_user=a.get("last_user", ""), last_assistant=a.get("last_assistant", ""),
                screen=screen, screen_hash=t["screen_hash"], cols=pane["w"], rows=pane["h"],
                attached=t["attached"], windows=t["windows"], resume_id=a.get("id") or "",
                cmd=pane["cmd"], home=home, model=a.get("model", ""), effort=a.get("effort", ""),
                ctx=a.get("ctx") or 0, ctx_window=a.get("ctx_window") or 0, git=t.get("git"),
                has_title=bool(a.get("has_title")),
            )
        for b in snap.get("bare", []):
            sid = b.get("id") or f"pid{b.get('pid')}"
            live_ids.add(sid)
            key = f"{host}/bare/{b['kind']}/{sid}"
            out[key] = Session(
                key=key, host=host, kind="bare", agent=b["kind"],
                title=b.get("title") or b.get("name") or "", cwd=b.get("cwd") or "",
                status=b.get("status"), updated=b.get("updated") or 0,
                last_user=b.get("last_user", ""), last_assistant=b.get("last_assistant", ""),
                resume_id=b.get("id") or "", tty=b.get("tty", ""), home=home,
                background=b.get("entrypoint") not in (None, "cli"),
                model=b.get("model", ""), effort=b.get("effort", ""),
                ctx=b.get("ctx") or 0, ctx_window=b.get("ctx_window") or 0, git=b.get("git"),
            )
        prev = self.by_host.get(host, {})
        changed = False
        for k, sess in out.items():
            old = prev.get(k)
            if old and old.status == "busy" and sess.status == "idle":
                self.state["idle_at"][k] = time.time()
                self.finished.append(sess)
                changed = True
            if sess.status == "approval" and (old is None or old.status != "approval"):
                self.attention.append(sess)
        if changed:
            self.save()
        self.by_host[host] = out
        live = {k for k in out}
        self.screens = {k: v for k, v in self.screens.items() if k in live or not k.startswith(host + "/")}
        hide = self.cfg.ui.get("hide_cwd", [])
        if snap.get("history") is not None:
            hist = []
            for h in snap["history"]:
                if any(p in (h.get("cwd") or "") for p in hide):
                    continue
                hist.append(self.history_session(host, h, home))
            self.history[host] = hist
        self.homes = getattr(self, "homes", {})
        self.homes[host] = home
        self._live_ids = getattr(self, "_live_ids", {})
        self._live_ids[host] = live_ids
        if host == self.prefs_host and snap.get("prefs") is not None:
            self.apply_prefs(snap["prefs"])
        if snap.get("codex_limits"):
            self.codex_limits[host] = snap["codex_limits"]

    def history_session(self, host, h, home) -> Session:
        return Session(
            key=f"{host}/history/{h['kind']}/{h['id']}", host=host, kind="history",
            agent=h["kind"], title=h.get("title") or "", cwd=h.get("cwd") or "",
            updated=h.get("mtime") or 0, last_user=h.get("last_user", ""),
            last_assistant=h.get("last_assistant", ""), resume_id=h["id"], home=home,
            model=h.get("model", ""), effort=h.get("effort", ""),
            ctx=h.get("ctx") or 0, ctx_window=h.get("ctx_window") or 0, snippet=h.get("snippet", ""),
        )

    def recent_dirs(self, host: str, limit=40) -> list[str]:
        """Directories used on `host`, most recent first, as ~/... (valid on that host)."""
        hide = self.cfg.ui.get("hide_cwd", [])
        pool = list(self.by_host.get(host, {}).values()) + self.history.get(host, [])
        out = []
        for x in sorted(pool, key=lambda x: -(x.updated or 0)):
            d = x.short_cwd
            if d and d not in out and not any(p in (x.cwd or "") for p in hide):
                out.append(d)
        return out[:limit] or ["~"]

    def set_search(self, host, q, results):
        if q != self.search_q:
            return  # stale reply
        hide = self.cfg.ui.get("hide_cwd", [])
        home = getattr(self, "homes", {}).get(host, "")
        self.search[host] = [self.history_session(host, h, home) for h in results
                             if not any(p in (h.get("cwd") or "") for p in hide)]

    # ---------------------------------------------------------------- views
    def live(self, host: str | None = None, show_background=False) -> list[Session]:
        out = []
        for h in self.hosts:
            if host and h != host:
                continue
            hits = {x.resume_id: x.snippet for x in self.search.get(h, [])} if self.search_q else {}
            for s in self.by_host[h].values():
                s.snippet = hits.get(s.resume_id, "") if s.resume_id else ""
                if s.background and not show_background:
                    continue
                if s.ident in self.state["hidden"] and not self.show_hidden:
                    continue
                s.offline = not self.host_state.get(h, "").startswith("online")
                out.append(s)
        return self.order(out)

    def recent(self, host: str | None = None) -> list[Session]:
        live = getattr(self, "_live_ids", {})
        out = []
        for h in self.hosts:
            if host and h != host:
                continue
            pool = self.history[h]
            if self.search_q and self.search.get(h):
                # transcript hits first, then the usual recent list
                hits = {s.resume_id: s for s in self.search[h]}
                pool = list(hits.values()) + [s for s in pool if s.resume_id not in hits]
            out += [s for s in pool if s.resume_id not in live.get(h, ())
                    and (self.show_hidden or s.ident not in self.state["hidden"])]
        return sorted(out, key=lambda s: (not s.snippet, -s.updated))

    def order(self, sessions: list[Session]) -> list[Session]:
        hi = {h: i for i, h in enumerate(self.hosts)}
        pinned = self.state["pinned"]
        pin = lambda s: pinned.index(s.ident) if s.ident in pinned else len(pinned)  # noqa: E731
        if self.cfg.ui.get("order") == "stable":
            return sorted(sessions, key=lambda s: (pin(s), hi.get(s.host, 99), s.kind != "tmux", s.created, s.key))
        opened = self.state["opened"]
        # MRU: pinned first (in pin order), then opened-through-termwall, then activity
        return sorted(sessions, key=lambda s: (pin(s), -(opened.get(s.key) or 0), s.kind != "tmux", -s.updated))


    # ---------------------------------------------------------------- reboot survival
    def track(self, host: str, snap: dict) -> list[dict]:
        """Remember this host's tmux sessions. Returns the specs to recreate when
        the host has rebooted since we last saw it (new boot time)."""
        boot = snap.get("boot") or 0
        mem = self.state["remember"].setdefault(host, {"boot": boot, "sessions": {}})
        remembered = mem["sessions"]
        current = {s.name: s for s in self.by_host[host].values() if s.kind == "tmux"}
        restore = []
        if boot and mem["boot"] and boot != mem["boot"]:
            restore = [dict(spec, name=n) for n, spec in remembered.items() if n not in current]
            remembered.clear()
        else:
            # same boot: a session that vanished was quit/killed on purpose
            for n in [n for n in remembered if n not in current]:
                del remembered[n]
        for n, s in current.items():
            spec = {"cwd": s.cwd, "agent": s.agent, "resume_id": s.resume_id}
            old = remembered.get(n, {})
            if not spec["resume_id"] and old.get("agent") == s.agent:
                spec["resume_id"] = old.get("resume_id", "")  # keep id across a brief gap
            remembered[n] = spec
        changed = restore or mem["boot"] != boot or remembered != getattr(self, "_last_saved_mem", {}).get(host)
        mem["boot"] = boot
        if changed:
            self._last_saved_mem = {**getattr(self, "_last_saved_mem", {}), host: dict(remembered)}
            self.save()
        return restore

    # ---------------------------------------------------------------- codex usage
    def codex_usage(self) -> list[str]:
        """Human lines for the newest Codex rate-limit snapshot across hosts."""
        snaps = [v for v in self.codex_limits.values() if v]
        if not snaps:
            return []
        lim = max(snaps, key=lambda v: v.get("ts") or "")
        out = []
        for key in ("primary", "secondary"):
            w = lim.get(key)
            if not w or w.get("used_percent") is None:
                continue
            mins = w.get("window_minutes") or 0
            span = ("weekly" if mins >= 7 * 1440 else f"{mins // 60}-hour" if mins >= 60 else f"{mins}-min")
            reset = ""
            if w.get("resets_at"):
                reset = " \u00b7 resets " + time.strftime("%a %-I:%M%p", time.localtime(w["resets_at"])).replace("AM", "am").replace("PM", "pm")
            out.append(f"Codex {span}: {w['used_percent']:.0f}% used{reset}")
        return out

    # ---------------------------------------------------------------- stale sessions
    def is_stale(self, s: Session) -> bool:
        days = float(self.cfg.ui.get("stale_days", 7) or 0)
        return (days > 0 and s.kind == "tmux" and not s.attached and s.status not in ("busy", "approval")
                and bool(s.updated) and s.updated < time.time() - days * 86400
                and s.ident not in self.state["pinned"])

    def stale(self) -> list[Session]:
        return [s for h in self.hosts for s in self.by_host[h].values() if self.is_stale(s)]
