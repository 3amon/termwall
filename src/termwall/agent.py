"""termwall host agent.

Runs on every host (locally or via `ssh host python3 -c ...`), stdlib only,
Python 3.8+ (macOS CLT python is old). Discovers:

  * tmux sessions (+ a capture of the active pane's screen)
  * Claude Code sessions (~/.claude/sessions/<pid>.json + transcripts)
  * Codex sessions (running `codex` processes + ~/.codex/sessions rollouts)
  * recent ended sessions of both, for "resume in tmux"

`--stream N` prints one JSON snapshot per line every N seconds and accepts
JSON command lines on stdin ({"op": "new" | "kill" | "rename" | "interval" |
"refresh", ...}). Screens are only resent when they change.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
os.environ["PATH"] = os.pathsep.join(
    [p for p in ("/opt/homebrew/bin", "/usr/local/bin", os.path.join(HOME, ".local/bin"))
     if os.path.isdir(p)] + [os.environ.get("PATH", "")])
IS_MAC = sys.platform == "darwin"


def leaked_agent_var(k):
    """Env set by a *running* Claude Code / Codex for its children. If termwall
    (or tmux) was started from inside one, new sessions must not inherit it."""
    return (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_PID", "CLAUDE_EFFORT")
            or k.startswith("CODEX_THREAD") or k.startswith("CODEX_SANDBOX"))


for _k in [k for k in os.environ if leaked_agent_var(k)]:
    del os.environ[_k]
TAIL_BYTES = 192 * 1024
HISTORY_DAYS = 21
HISTORY_MAX = 40
SNIP = 2000
PREFS_PATH = os.path.join(HOME, ".config", "termwall", "prefs.json")
PREF_KEYS = ("pinned", "hidden", "named")


def run(args, timeout=5):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------- processes

def proc_table():
    """pid -> (ppid, tty, args)."""
    out = run(["ps", "-axo", "pid=,ppid=,tty=,command="])
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
        except ValueError:
            pass
    return procs


def descendants(procs, root):
    kids = {}
    for pid, (ppid, _, _) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        for k in kids.get(p, ()):
            out.append(k)
            stack.append(k)
    return out


def is_codex_tui(args):
    argv0 = args.split()[0] if args else ""
    base = os.path.basename(argv0)
    if "app-server" in args or "mcp" in args.split()[1:2]:
        return False
    if base == "codex":
        return True
    # npm shim: `node /opt/homebrew/bin/codex ...`
    return base == "node" and re.search(r"/codex(\s|$)", args) is not None


def proc_age(pid):
    """Seconds since the process started (ps etime: [[dd-]hh:]mm:ss)."""
    out = run(["ps", "-o", "etime=", "-p", str(pid)]).strip()
    days, _, rest = out.rpartition("-")
    secs = 0
    for part in rest.split(":"):
        secs = secs * 60 + int(part or 0) if part.isdigit() else secs
    return secs + int(days or 0) * 86400 if out else 0


def proc_cwd(pid):
    if not IS_MAC:
        try:
            return os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            return None
    out = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=3)
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def proc_open_rollout(pid):
    if not IS_MAC:
        try:
            for fd in os.listdir("/proc/%d/fd" % pid):
                try:
                    t = os.readlink("/proc/%d/fd/%s" % (pid, fd))
                except OSError:
                    continue
                if "/.codex/sessions/" in t and t.endswith(".jsonl"):
                    return t
        except OSError:
            pass
        return None
    out = run(["lsof", "-p", str(pid), "-Fn"], timeout=3)
    for line in out.splitlines():
        if line.startswith("n") and "/.codex/sessions/" in line and line.endswith(".jsonl"):
            return line[1:]
    return None


# ---------------------------------------------------------------- transcripts

def read_tail(path, nbytes=TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - nbytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    out = []
    for l in lines:
        if not l.strip():
            continue
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def read_head(path, nbytes=64 * 1024):
    try:
        with open(path, "rb") as f:
            data = f.read(nbytes)
    except OSError:
        return []
    out = []
    for l in data.split(b"\n")[:-1]:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def clean(text, n=SNIP):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"\s+\n", "\n", text).strip()
    if len(text) > n:
        text = text[: n - 1] + "…"
    return text


def _claude_text(content, want_user):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)


def _claude_user_prompt(d):
    if d.get("isMeta") or d.get("isSidechain"):
        return ""
    msg = d.get("message") or {}
    t = _claude_text(msg.get("content"), True).strip()
    if not t or t.startswith("<") or t.startswith("[Request interrupted"):
        return ""
    return t


_cache = {}


def cached(path, fn):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, fn.__name__)
    hit = _cache.get(key)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    val = fn(path)
    if val is not None:
        val["mtime"] = st.st_mtime
    _cache[key] = ((st.st_mtime, st.st_size), val)
    return val


def parse_claude(path):
    title = custom = last_prompt = last_asst = last_user = ""
    model = effort = ""
    ctx = 0
    cwd = None
    busy_hint = None
    entries = read_tail(path)
    for d in entries:
        t = d.get("type")
        if t == "ai-title":
            title = d.get("aiTitle") or title
        elif t in ("custom-title", "customTitle"):
            custom = d.get("customTitle") or d.get("title") or custom
        elif t == "summary" and not title:
            title = d.get("summary") or title
        elif t == "last-prompt":
            last_prompt = d.get("lastPrompt") or last_prompt
        elif t == "assistant":
            msg = d.get("message") or {}
            if msg.get("model") and not msg["model"].startswith("<") and not d.get("isSidechain"):
                model = msg["model"]
                effort = d.get("effort") or effort
                u = msg.get("usage") or {}
                used = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                  "cache_read_input_tokens"))
                ctx = used or ctx
            txt = _claude_text(msg.get("content"), False).strip()
            if txt and not d.get("isSidechain"):
                last_asst = txt
            busy_hint = "assistant"
        elif t == "user":
            p = _claude_user_prompt(d)
            if p:
                last_user = p
        if d.get("cwd"):
            cwd = d["cwd"]
        if t == "system" and d.get("subtype") == "turn_duration":
            busy_hint = "done"
    # resume must run where the session started (that's the project dir its
    # transcript is filed under), not wherever it later cd'd to
    start_cwd = None
    for d in read_head(path):
        if d.get("cwd"):
            start_cwd = d["cwd"]
            break
    cwd = start_cwd or cwd
    if not (title or custom):
        for d in read_head(path):
            if d.get("type") == "user" and not last_user:
                last_user = _claude_user_prompt(d)
            if d.get("type") == "ai-title" and not title:
                title = d.get("aiTitle") or ""
    if not (last_user or last_asst or last_prompt):
        return None  # empty / aborted session, not interesting
    return {
        "id": os.path.basename(path)[:-6],
        "title": clean(custom or title or last_prompt or last_user, 120),
        "has_title": bool(custom or title),
        "cwd": cwd,
        "last_user": clean(last_user or last_prompt),
        "last_assistant": clean(last_asst),
        "model": model,
        "effort": effort,
        "ctx": ctx,
        "ctx_window": 0,   # claude doesn't log it; the overlay uses the configured size
        "turn": busy_hint,
        "path": path,
    }


def codex_index():
    names = {}
    p = os.path.join(HOME, ".codex", "session_index.jsonl")
    try:
        with open(p) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("id") and d.get("thread_name"):
                    names[d["id"]] = d["thread_name"]
    except OSError:
        pass
    return names


def _codex_msg_text(payload):
    parts = []
    for b in payload.get("content") or []:
        if isinstance(b, dict) and b.get("type") in ("output_text", "input_text", "text"):
            parts.append(b.get("text", ""))
    return "\n".join(parts).strip()


def _codex_user_ok(t):
    return t and not t.startswith("<") and not t.startswith("# AGENTS.md")


def parse_codex(path):
    sid = cwd = None
    last_user = last_asst = model = effort = ""
    ctx = ctx_window = 0
    limits = None
    state = None
    head = read_head(path, 16 * 1024)
    for d in head:
        if d.get("type") == "session_meta":
            p = d.get("payload") or {}
            sid = p.get("id") or p.get("session_id")
            cwd = p.get("cwd")
            break
    for d in read_tail(path):
        t = d.get("type")
        p = d.get("payload") or {}
        if t == "response_item" and p.get("type") == "message":
            txt = _codex_msg_text(p)
            if p.get("role") == "assistant" and txt:
                last_asst = txt
            elif p.get("role") == "user" and _codex_user_ok(txt):
                last_user = txt
        elif t == "event_msg":
            et = p.get("type")
            if et == "task_started":
                state = "busy"
            elif et in ("task_complete", "turn_aborted"):
                state = "idle"
            elif et == "token_count" and p.get("info"):
                info = p["info"]
                ctx = (info.get("last_token_usage") or {}).get("input_tokens") or ctx
                ctx_window = info.get("model_context_window") or ctx_window
            if et == "token_count" and p.get("rate_limits"):
                limits = dict(p["rate_limits"], ts=d.get("timestamp"))
        elif t == "turn_context":
            cwd = p.get("cwd") or cwd
            model = p.get("model") or model
            effort = p.get("effort") or effort
    if not sid:
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path)
        sid = m.group(1) if m else os.path.basename(path)
    if not (last_user or last_asst):
        return None
    return {
        "id": sid,
        "title": clean(last_user, 120),
        "cwd": cwd,
        "last_user": clean(last_user),
        "last_assistant": clean(last_asst),
        "model": model,
        "effort": effort,
        "ctx": ctx,
        "ctx_window": ctx_window,
        "limits": limits,
        "turn": state,
        "path": path,
    }


def codex_rollouts(limit):
    root = os.path.join(HOME, ".codex", "sessions")
    files = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return files[:limit]


def claude_transcript_path(session_id):
    hits = glob.glob(os.path.join(HOME, ".claude", "projects", "*", session_id + ".jsonl"))
    return hits[0] if hits else None


_git_cache = {}


def git_info(cwd):
    """{repo, branch, common} for the repo containing cwd, from .git files (no subprocess)."""
    if not cwd:
        return None
    hit = _git_cache.get(cwd)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    info = None
    d = cwd
    while d and d != os.path.dirname(d):
        g = os.path.join(d, ".git")
        if os.path.exists(g):
            gitdir = g
            if os.path.isfile(g):  # worktree / submodule: "gitdir: <path>"
                try:
                    with open(g) as f:
                        line = f.read().strip()
                    gitdir = os.path.normpath(os.path.join(d, line.split(":", 1)[1].strip()))
                except (OSError, IndexError):
                    break
            common = gitdir
            try:
                with open(os.path.join(gitdir, "commondir")) as f:
                    common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
            except OSError:
                pass
            branch = ""
            try:
                with open(os.path.join(gitdir, "HEAD")) as f:
                    head = f.read().strip()
                branch = head[16:] if head.startswith("ref: refs/heads/") else head[:8]
            except OSError:
                pass
            repo = os.path.basename(os.path.dirname(common)) if common.endswith(".git") else os.path.basename(d)
            info = {"repo": repo, "branch": branch, "common": common, "worktree": d}
            break
        d = os.path.dirname(d)
    _git_cache[cwd] = (time.time(), info)
    return info


def boot_time():
    """Host boot time (epoch s) -- tells a reboot apart from a session you quit."""
    try:
        if IS_MAC:
            m = re.search(r"sec = (\d+)", run(["sysctl", "-n", "kern.boottime"]))
            return int(m.group(1)) if m else 0
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


BOOT_TIME = boot_time()


# ---------------------------------------------------------------- tmux

SEP = "|::|"
TMUX_FMT = SEP.join([
    "#{session_id}", "#{session_name}", "#{session_created}", "#{session_activity}",
    "#{session_attached}", "#{session_windows}", "#{window_index}", "#{window_name}",
    "#{window_active}", "#{pane_id}", "#{pane_pid}", "#{pane_active}",
    "#{pane_current_command}", "#{pane_current_path}", "#{pane_width}", "#{pane_height}",
    "#{pane_title}", "#{window_activity}",
])


def tmux_panes():
    tm = shutil.which("tmux")
    if not tm:
        return None, []
    out = run([tm, "list-panes", "-a", "-F", TMUX_FMT])
    sessions = {}
    for line in out.splitlines():
        f = line.split(SEP)
        if len(f) < 18:
            continue
        sid = f[0]
        s = sessions.setdefault(sid, {
            "id": sid, "name": f[1], "created": int(f[2] or 0),
            "activity": int(f[3] or 0), "attached": int(f[4] or 0),
            "windows": int(f[5] or 0), "panes": [],
        })
        s["activity"] = max(s["activity"], int(f[17] or 0))
        s["panes"].append({
            "window": int(f[6]), "window_name": f[7], "window_active": f[8] == "1",
            "pane": f[9], "pid": int(f[10] or 0), "active": f[11] == "1",
            "cmd": f[12], "cwd": f[13], "w": int(f[14] or 80), "h": int(f[15] or 24),
            "title": f[16],
        })
    return tm, list(sessions.values())


def capture(tm, pane):
    return run([tm, "capture-pane", "-p", "-e", "-t", pane], timeout=3)


# ---------------------------------------------------------------- snapshot

class Agent:
    def __init__(self):
        self.sent_screens = {}  # pane -> hash
        self.history = []
        self.history_at = 0
        self.tick = 0
        self.prefs_sent = None

    def claude_live(self):
        out = []
        for p in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
            try:
                with open(p) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            pid = d.get("pid")
            try:
                os.kill(pid, 0)
            except (OSError, TypeError):
                continue
            out.append(d)
        return out

    def snapshot(self, full=False):
        self.tick += 1
        procs = proc_table()
        tm, sessions = tmux_panes()
        claude = {d["pid"]: d for d in self.claude_live()}
        codex_pids = {pid: v for pid, v in procs.items() if is_codex_tui(v[2])}
        # one codex per tty (npm shim + native binary share it): keep the deepest
        by_tty = {}
        for pid, (ppid, tty, args) in codex_pids.items():
            if ppid in codex_pids:
                continue
            by_tty[tty if tty not in ("?", "??") else pid] = pid
        seen_claude, seen_codex = set(), set()

        def agent_for(pids):
            for pid in pids:
                if pid in claude:
                    seen_claude.add(pid)
                    return self.claude_info(claude[pid])
            for pid in pids:
                if pid in codex_pids and codex_pids[pid][0] not in codex_pids:
                    seen_codex.add(pid)
                    return self.codex_info(pid, procs)
            return None

        for s in sessions:
            s["panes"].sort(key=lambda p: (not p["window_active"], not p["active"]))
            pane = s["panes"][0]
            s["pane"] = pane
            s["agent"] = agent_for([pane["pid"]] + descendants(procs, pane["pid"]))
            if not s["agent"]:
                for other in s["panes"][1:]:
                    a = agent_for([other["pid"]] + descendants(procs, other["pid"]))
                    if a:
                        s["agent"] = a
                        break
            s["git"] = git_info((s["agent"] or {}).get("cwd") or pane["cwd"])
            scr = capture(tm, pane["pane"])
            h = hashlib.md5(scr.encode("utf-8", "replace")).hexdigest()
            if full or self.sent_screens.get(pane["pane"]) != h:
                s["screen"] = scr
                self.sent_screens[pane["pane"]] = h
            else:
                s["screen"] = None
            s["screen_hash"] = h
            del s["panes"]

        bare = []
        for pid, d in claude.items():
            if pid in seen_claude:
                continue
            info = self.claude_info(d)
            info["tty"] = procs.get(pid, (0, "?", ""))[1]
            info["git"] = git_info(info.get("cwd"))
            bare.append(info)
        for tty, pid in by_tty.items():
            if pid in seen_codex:
                continue
            info = self.codex_info(pid, procs)
            info["tty"] = procs.get(pid, (0, "?", ""))[1]
            info["git"] = git_info(info.get("cwd"))
            bare.append(info)

        if full or time.time() - self.history_at > 20:
            self.history = self.scan_history()
            self.history_at = time.time()
            history = self.history
        else:
            history = None  # unchanged

        return {
            "type": "snapshot",
            "host": socket.gethostname().split(".")[0],
            "platform": sys.platform,
            "boot": BOOT_TIME,
            "now": time.time(),
            "tmux": tm,
            "home": HOME,
            "sessions": sessions,
            "bare": bare,
            "history": history,
            "codex_limits": self.codex_limits(),
            "prefs": self.prefs_if_changed(full),
        }

    def prefs_if_changed(self, full):
        """Shared UI prefs (pins/hidden/named) kept on this host; None = unchanged."""
        try:
            st = os.stat(PREFS_PATH)
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = None
        if not full and sig == self.prefs_sent:
            return None
        self.prefs_sent = sig
        return read_prefs()

    def codex_limits(self):
        """Rate-limit usage from the newest Codex rollout on this host (it's account-wide)."""
        for p in codex_rollouts(4):
            t = cached(p, parse_codex)
            if t and t.get("limits"):
                return t["limits"]
        return None

    def claude_info(self, d):
        info = {
            "kind": "claude", "id": d.get("sessionId"), "pid": d.get("pid"),
            "status": d.get("status"), "name": d.get("name"),
            "cwd": d.get("cwd"), "entrypoint": d.get("entrypoint"),
            "updated": (d.get("updatedAt") or d.get("startedAt") or 0) / 1000.0,
        }
        path = claude_transcript_path(d.get("sessionId") or "-")
        t = cached(path, parse_claude) if path else None
        if t:
            info.update({k: t[k] for k in ("title", "last_user", "last_assistant", "model", "effort", "ctx", "ctx_window",
                                         "has_title")})
            info["updated"] = max(info["updated"], t["mtime"])
        return info

    def codex_info(self, pid, procs):
        path = proc_open_rollout(pid)
        cwd = proc_cwd(pid)
        if not path:
            # `codex resume <id>` names its conversation on the command line; a resumed
            # codex doesn't touch its rollout until the next turn, so mtime can't tell
            m = UUID_RE.search(procs.get(pid, (0, "", ""))[2])
            if m:
                path = codex_rollout_path(m.group(0))
        if not path:
            # newest rollout in this cwd written since the process started
            # (a fresh codex has no rollout until the first prompt)
            started = time.time() - proc_age(pid)
            for p in codex_rollouts(15):
                meta = cached(p, parse_codex)
                if meta and meta.get("cwd") == cwd and meta["mtime"] >= started:
                    path = p
                    break
        info = {"kind": "codex", "pid": pid, "cwd": cwd, "status": None, "updated": 0}
        t = cached(path, parse_codex) if path else None
        if t:
            info.update({k: t[k] for k in ("id", "title", "last_user", "last_assistant", "model", "effort", "ctx", "ctx_window")})
            info["status"] = t["turn"]
            info["updated"] = t["mtime"]
            names = codex_index()
            if names.get(t["id"]):
                info["title"] = names[t["id"]]
                info["has_title"] = True
        return info

    def scan_history(self):
        cutoff = time.time() - HISTORY_DAYS * 86400
        out = []
        files = glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))
        files = [(os.path.getmtime(p), p) for p in files]
        files = sorted([x for x in files if x[0] > cutoff], reverse=True)[:HISTORY_MAX]
        for _, p in files:
            t = cached(p, parse_claude)
            if t:
                out.append(dict(t, kind="claude"))
        names = codex_index()
        for p in codex_rollouts(HISTORY_MAX):
            if os.path.getmtime(p) < cutoff:
                continue
            t = cached(p, parse_codex)
            if t:
                t = dict(t, kind="codex")
                if names.get(t["id"]):
                    t["title"] = names[t["id"]]
                out.append(t)
        for t in out:
            t.pop("path", None)
            t.pop("turn", None)
            t.pop("limits", None)
        out.sort(key=lambda t: t["mtime"], reverse=True)
        return out[:HISTORY_MAX]


# ---------------------------------------------------------------- commands

TMUX_DEFAULTS = [
    ["set", "-g", "mouse", "on"],
    ["set", "-g", "history-limit", "50000"],
    ["set", "-g", "set-titles", "on"],
    ["set", "-g", "set-titles-string", "#S · #H"],
    ["set", "-g", "status-style", "bg=#1f2335,fg=#a9b1d6"],
    ["set", "-g", "status-left", "#[fg=#1a1b26,bg=#7aa2f7,bold] #H #[default] "],
    ["set", "-g", "status-left-length", "30"],
    ["set", "-g", "status-right", "#[fg=#787c99]#S  %H:%M "],
    ["set", "-g", "escape-time", "10"],
    ["set", "-g", "focus-events", "on"],
    ["set", "-as", "terminal-features", ",xterm-kitty:RGB,xterm-256color:RGB"],
]

AGENT_CMDS = {"shell": None}


def cmd_new(c):
    tm = shutil.which("tmux")
    if not tm:
        return {"ok": False, "error": "tmux not installed on this host"}
    cwd = os.path.expanduser(c.get("cwd") or "~")
    if not os.path.isdir(cwd):
        return {"ok": False, "error": "no such directory: %s" % cwd}
    name = re.sub(r"[.:]", "-", c.get("name") or os.path.basename(cwd.rstrip("/")) or "session")
    existing = set(run([tm, "list-sessions", "-F", "#{session_name}"]).split())
    base, i = name, 2
    while name in existing:
        name = "%s-%d" % (base, i)
        i += 1
    r = subprocess.run([tm, "new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50"],
                       stderr=subprocess.PIPE)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.decode()}
    for opt in TMUX_DEFAULTS:
        run([tm] + opt)
    for line in run([tm, "show-environment", "-g"]).splitlines():
        k = line.split("=", 1)[0].lstrip("-")
        if leaked_agent_var(k):
            run([tm, "set-environment", "-g", "-u", k])
            run([tm, "set-environment", "-t", "=" + name, "-u", k])
    command = c.get("command")
    if command:
        run([tm, "send-keys", "-t", "=" + name + ":", command, "Enter"])
    sid = run([tm, "display-message", "-p", "-t", "=" + name + ":", "#{session_id}"]).strip()
    return {"ok": True, "name": name, "session_id": sid}


def cmd_kill(c):
    tm = shutil.which("tmux")
    r = subprocess.run([tm, "kill-session", "-t", c["target"]], stderr=subprocess.PIPE)
    return {"ok": r.returncode == 0, "error": r.stderr.decode()}


def cmd_rename(c):
    tm = shutil.which("tmux")
    r = subprocess.run([tm, "rename-session", "-t", c["target"], c["name"]], stderr=subprocess.PIPE)
    return {"ok": r.returncode == 0, "error": r.stderr.decode()}


def cmd_send(c):
    """Type text into a session (quick reply). Empty text = just Enter."""
    tm = shutil.which("tmux")
    target = c["target"] + ":" if c["target"].startswith("$") else c["target"]
    text = c.get("text") or ""
    if text:
        r = subprocess.run([tm, "send-keys", "-t", target, "-l", "--", text], stderr=subprocess.PIPE)
        if r.returncode:
            return {"ok": False, "error": r.stderr.decode()}
        time.sleep(0.05)  # let the TUI take the paste before Enter
    if c.get("enter", True):
        subprocess.run([tm, "send-keys", "-t", target, "Enter"])
    return {"ok": True}


def cmd_keys(c):
    """Press named keys (tmux key names) in a session: Escape, BTab, C-c, Up..."""
    tm = shutil.which("tmux")
    target = c["target"] + ":" if c["target"].startswith("$") else c["target"]
    for k in c.get("keys", [])[:8]:
        if re.match(r"^[A-Za-z0-9-]{1,12}$", k):
            subprocess.run([tm, "send-keys", "-t", target, k])
    return {"ok": True}


def read_prefs():
    try:
        with open(PREFS_PATH) as f:
            d = json.load(f)
        return {k: list(d.get(k, [])) for k in PREF_KEYS}
    except (OSError, ValueError):
        return {k: [] for k in PREF_KEYS}


def cmd_prefs_set(c):
    """Add/remove entries: {"add": {"pinned": [...]}, "remove": {"hidden": [...]}} (atomic write)."""
    d = read_prefs()
    for k, vals in (c.get("add") or {}).items():
        if k in PREF_KEYS:
            d[k] += [v for v in vals if v not in d[k]]
    for k, vals in (c.get("remove") or {}).items():
        if k in PREF_KEYS:
            d[k] = [v for v in d[k] if v not in vals]
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, PREFS_PATH)
    return {"ok": True, "prefs": d}


def cmd_presence(c):
    """Is someone at this machine? {idle: seconds, locked: bool}"""
    idle, locked = 0.0, False
    if IS_MAC:
        m = re.search(r'"HIDIdleTime" = (\d+)', run(["ioreg", "-c", "IOHIDSystem"]))
        idle = int(m.group(1)) / 1e9 if m else 0.0
    else:
        env = dict(os.environ)
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/%d/bus" % os.getuid())

        def gdbus(dest, path, method):
            try:
                return subprocess.run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path,
                                       "--method", method], capture_output=True, text=True, env=env,
                                      timeout=3).stdout
            except Exception:
                return ""
        m = re.search(r"(\d+)", gdbus("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                                      "org.gnome.Mutter.IdleMonitor.GetIdletime"))
        idle = int(m.group(1)) / 1000.0 if m else 0.0
        locked = "true" in gdbus("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                 "org.gnome.ScreenSaver.GetActive")
    return {"ok": True, "idle": idle, "locked": locked}


def cmd_capture(c):
    """Scrollback + screen of a session's active pane."""
    tm = shutil.which("tmux")
    target = c["target"] + ":" if c["target"].startswith("$") else c["target"]
    n = int(c.get("lines", 1000))
    out = run([tm, "capture-pane", "-p", "-e", "-J", "-S", str(-n), "-t", target], timeout=5)
    return {"ok": True, "screen": out}


def _claude_messages(path, limit):
    out = []
    for d in read_tail(path, 3 * 1024 * 1024):
        t = d.get("type")
        if d.get("isSidechain"):
            continue
        if t == "user":
            txt = _claude_user_prompt(d)
            if txt:
                out.append({"role": "user", "text": clean(txt, 4000), "ts": d.get("timestamp")})
        elif t == "assistant":
            txt = _claude_text((d.get("message") or {}).get("content"), False).strip()
            if txt:
                if out and out[-1]["role"] == "assistant":
                    out[-1]["text"] = clean(out[-1]["text"] + "\n\n" + txt, 6000)
                else:
                    out.append({"role": "assistant", "text": clean(txt, 6000), "ts": d.get("timestamp")})
    return out[-limit:]


def _codex_messages(path, limit):
    out = []
    for d in read_tail(path, 3 * 1024 * 1024):
        p = d.get("payload") or {}
        if d.get("type") == "response_item" and p.get("type") == "message":
            txt = _codex_msg_text(p)
            role = p.get("role")
            if role == "user" and _codex_user_ok(txt):
                out.append({"role": "user", "text": clean(txt, 4000), "ts": d.get("timestamp")})
            elif role == "assistant" and txt:
                out.append({"role": "assistant", "text": clean(txt, 6000), "ts": d.get("timestamp")})
    return out[-limit:]


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def codex_rollout_path(sid):
    for p in codex_rollouts(400):
        if sid in os.path.basename(p):
            return p
    return None


def cmd_transcript(c):
    kind, sid = c.get("kind"), c.get("id") or ""
    path = claude_transcript_path(sid) if kind == "claude" else codex_rollout_path(sid)
    if not path:
        return {"ok": False, "error": "transcript not found"}
    fn = _claude_messages if kind == "claude" else _codex_messages
    return {"ok": True, "messages": fn(path, int(c.get("limit", 60)))}


def _hit_text(d, kind):
    if kind == "claude":
        t = d.get("type")
        if d.get("isSidechain"):
            return ""
        if t == "user":
            return _claude_user_prompt(d)
        if t == "assistant":
            return _claude_text((d.get("message") or {}).get("content"), False)
        return ""
    p = d.get("payload") or {}
    if d.get("type") == "response_item" and p.get("type") == "message":
        txt = _codex_msg_text(p)
        return txt if (p.get("role") == "assistant" or _codex_user_ok(txt)) else ""
    return ""


def cmd_search(c):
    """Full-text search of recent transcripts. Returns history-style entries + snippet."""
    q = (c.get("q") or "").strip().lower()
    if len(q) < 3:
        return {"ok": True, "results": []}
    qb = q.encode()
    days = int(c.get("days", 60))
    skip = [x.lower() for x in c.get("skip", [])]
    cutoff = time.time() - days * 86400
    files = [(p, "claude") for p in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))]
    files += [(p, "codex") for p in codex_rollouts(400)]
    files = [(os.path.getmtime(p), p, k) for p, k in files if os.path.exists(p)]
    files = sorted([f for f in files if f[0] > cutoff and not any(x in f[1].lower() for x in skip)],
                   reverse=True)
    names = codex_index()
    results = []
    for mtime, path, kind in files:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        low = data.lower()
        if qb not in low:
            continue
        snippet = ""
        for line in reversed(data.split(b"\n")):
            if qb not in line.lower():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            txt = _hit_text(d, kind) or ""
            i = txt.lower().find(q)
            if i >= 0:
                a, b = max(0, i - 90), min(len(txt), i + len(q) + 160)
                snippet = ("\u2026" if a else "") + txt[a:b].replace("\n", " ") + ("\u2026" if b < len(txt) else "")
                break
        if not snippet:
            continue  # matched only in tool output / metadata
        meta = cached(path, parse_claude if kind == "claude" else parse_codex)
        if not meta:
            continue
        e = dict(meta, kind=kind, snippet=snippet)
        if kind == "codex" and names.get(e["id"]):
            e["title"] = names[e["id"]]
        e.pop("path", None)
        e.pop("turn", None)
        results.append(e)
        if len(results) >= 30:
            break
    return {"ok": True, "results": results, "q": q}


def cmd_listdir(c):
    path = os.path.expanduser(c.get("path") or "~")
    try:
        names = sorted(n for n in os.listdir(path)
                       if not n.startswith(".") and os.path.isdir(os.path.join(path, n)))
    except OSError:
        names = []
    return {"ok": True, "path": path, "dirs": names[:200]}


# ---------------------------------------------------------------- main

import threading  # noqa: E402

_emit_lock = threading.Lock()
SLOW_OPS = {"search", "transcript"}


def emit(obj):
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def main(argv):
    agent = Agent()
    if "--stream" not in argv:
        emit(agent.snapshot(full=True))
        return
    interval = float(argv[argv.index("--stream") + 1])
    full = True
    buf = b""
    fd = sys.stdin.fileno()
    while True:
        emit(agent.snapshot(full=full))
        full = False
        deadline = time.time() + interval
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                return  # controller went away
            buf += chunk
            refresh = False
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                op = c.get("op")
                if op == "interval":
                    interval = max(0.3, float(c.get("seconds", interval)))
                    refresh = True
                elif op == "refresh":
                    full = full or bool(c.get("full"))
                    refresh = True
                else:
                    fn = {"new": cmd_new, "kill": cmd_kill, "rename": cmd_rename,
                          "listdir": cmd_listdir, "send": cmd_send, "keys": cmd_keys, "presence": cmd_presence,
                          "prefs_set": cmd_prefs_set, "capture": cmd_capture,
                          "transcript": cmd_transcript, "search": cmd_search}.get(op)

                    def handle(c=c, fn=fn, op=op):
                        try:
                            res = fn(c) if fn else {"ok": False, "error": "unknown op %r" % op}
                        except Exception as e:  # report, never die
                            res = {"ok": False, "error": repr(e)}
                        res.update({"type": "reply", "rid": c.get("rid")})
                        emit(res)
                    if op in SLOW_OPS:
                        threading.Thread(target=handle, daemon=True).start()
                    else:
                        handle()
                        refresh = op not in ("capture",)
            if refresh:
                break


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (KeyboardInterrupt, BrokenPipeError):
        pass
