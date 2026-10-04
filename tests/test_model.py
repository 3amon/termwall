import time

import pytest

from termwall import config, model
from termwall.model import Session, Store, blocked_on_prompt


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "STATE_PATH", tmp_path / "state.json")
    cfg = config.Config(hosts=[config.Host("a"), config.Host("b", "b.example.net")],
                        agents={"claude_context": 1_000_000}, terminal={},
                        ui={"order": "mru", "stale_days": 7, "hide_cwd": []}, notify={})
    return Store(cfg)


# ---- prompts: real-world shapes -------------------------------------------------
SCREENS = {
    "claude permission": ("Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n"
                          "  2. Yes, and don't ask again for rm commands\n"
                          "  3. No, and tell Claude what to do differently (esc)", True),
    "codex permission": ("Would you like to run the following command?\n  $ git push\n"
                         "› 1. Yes, just this once\n  2. Yes, and don't ask again for this command\n"
                         "  3. No, and tell Codex what to do differently\nPress enter to confirm or esc to cancel", True),
    "claude trust": ("Quick safety check\n ❯ 1. Yes, I trust this folder\n   2. No, exit\n"
                     " Enter to confirm · Esc to cancel", True),
    "keychain": "❯ claude\npassword to unlock /Users/x/Library/Keychains/login.keychain-db: ",
    "sudo": "$ sudo apt update\n[sudo] password for dev: ",
    "shell y/n waiting": "Continue? (y/n) ",
    "codex update": "1. Update now\n2. Skip\nPress enter to continue",
}
QUIET = {
    "answered y/n": "Continue? (y/n) yes\ngot:[yes]\n$ ",
    "claude idle": "● Done.\n────────\n❯ \n────────\n  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    "codex idle": "• Worked for 2s\n› Ask Codex to do anything\n  GPT-6-Astra xhigh · ~/claude\n  ← for agents · ? for shortcuts",
    "agent talking about prompts": ("● I'll add a check for \"Do you want to proceed?\" dialogs.\n" + "x\n" * 7
                                    + "────────\n❯ \n────────\n  ⏵⏵ auto mode on"),
}


@pytest.mark.parametrize("name", list(SCREENS))
def test_blocked(name):
    screen = SCREENS[name] if isinstance(SCREENS[name], str) else SCREENS[name][0]
    assert blocked_on_prompt(screen)


@pytest.mark.parametrize("name", list(QUIET))
def test_not_blocked(name):
    assert not blocked_on_prompt(QUIET[name])


def test_ansi_is_ignored():
    assert blocked_on_prompt("\x1b[1mDo you want to proceed?\x1b[0m\n\x1b[36m❯ 1. Yes\x1b[0m")


# ---- labels ------------------------------------------------------------------------
@pytest.mark.parametrize("m,e,label", [
    ("claude-opus-5-5", "medium", "Opus 5.5 · medium"),
    ("claude-fable-5-1", "", "Fable 5.1"),
    ("claude-haiku-4-5-20251001", "low", "Haiku 4.5 · low"),
    ("gpt-6-astra", "xhigh", "GPT-6 Astra · xhigh"),
    ("", "", ""),
])
def test_model_label(m, e, label):
    assert Session(key="k", host="a", kind="tmux", agent="claude", model=m, effort=e).model_label == label


def test_ident_is_stable():
    s = Session(key="a/tmux/$3", host="a", kind="tmux", agent="claude", name="x", resume_id="abc")
    assert s.ident == "a/claude/abc"
    assert Session(key="a/tmux/$3", host="a", kind="tmux", agent="shell", name="sh").ident == "a/tmux/sh"


# ---- ingest / status -------------------------------------------------------------------
def snap(sessions=(), bare=(), history=None, boot=1000):
    return {"sessions": list(sessions), "bare": list(bare), "history": history, "home": "/home/x", "boot": boot}


def tmux(sid, name, screen="", agent=None, activity=None):
    return {"id": sid, "name": name, "created": 1, "activity": activity or int(time.time()), "attached": 0,
            "windows": 1, "pane": {"cmd": "bash", "cwd": "/w", "w": 80, "h": 24}, "agent": agent,
            "screen": screen, "screen_hash": str(hash(screen)), "git": None}


def test_status_transitions_and_needs_you(store):
    a = {"kind": "claude", "id": "c1", "status": "busy", "cwd": "/w", "ctx": 500_000}
    store.ingest("a", snap([tmux("$1", "w", agent=a)]))
    s = store.by_host["a"]["a/tmux/$1"]
    assert s.status == "busy" and not store.needs_you(s)
    assert store.ctx_frac(s) == 0.5
    store.ingest("a", snap([tmux("$1", "w", agent=dict(a, status="idle"))]))
    assert [x.key for x in store.finished] == ["a/tmux/$1"]
    assert store.needs_you(store.by_host["a"]["a/tmux/$1"])
    store.mark_opened(store.by_host["a"]["a/tmux/$1"])
    assert not store.needs_you(store.by_host["a"]["a/tmux/$1"])
    store.ingest("a", snap([tmux("$1", "w", screen="Do you want to proceed?\n❯ 1. Yes", agent=dict(a, status="idle"))]))
    s = store.by_host["a"]["a/tmux/$1"]
    assert s.status == "approval" and store.needs_you(s)
    assert [x.key for x in store.attention] == ["a/tmux/$1"]


def test_reboot_restore(store):
    agent = {"kind": "claude", "id": "c1", "status": "idle", "cwd": "/w"}
    store.ingest("a", snap([tmux("$1", "keep", agent=agent), tmux("$2", "quit-me")], boot=1000))
    assert store.track("a", snap(boot=1000)) == []
    # quitting a session on the same boot forgets it
    store.ingest("a", snap([tmux("$1", "keep", agent=agent)], boot=1000))
    assert store.track("a", snap(boot=1000)) == []
    # new boot: what was there comes back, with its resume id
    store.ingest("a", snap([], boot=2000))
    restore = store.track("a", snap(boot=2000))
    assert restore == [{"name": "keep", "cwd": "/w", "agent": "claude", "resume_id": "c1"}]
    assert store.track("a", snap(boot=2000)) == []      # only once


def test_pin_hide_and_order(store):
    store.ingest("a", snap([tmux("$1", "one"), tmux("$2", "two")]))
    two = store.by_host["a"]["a/tmux/$2"]
    store.toggle("pinned", two)
    assert [s.name for s in store.live()][0] == "two"
    store.toggle("hidden", two)
    assert "two" not in [s.name for s in store.live()]
    store.show_hidden = True
    assert "two" in [s.name for s in store.live()]


def test_stale(store):
    old = int(time.time()) - 10 * 86400
    store.ingest("a", snap([tmux("$1", "old", activity=old), tmux("$2", "fresh")]))
    assert [s.name for s in store.stale()] == ["old"]
    store.toggle("pinned", store.by_host["a"]["a/tmux/$1"])
    assert store.stale() == []


def test_search_hits(store):
    store.ingest("a", snap([tmux("$1", "w", agent={"kind": "claude", "id": "live1", "status": "idle"})],
                           history=[{"kind": "claude", "id": "old1", "title": "old", "mtime": 1}]))
    store.search_q = "radio"
    store.set_search("a", "radio", [{"kind": "claude", "id": "live1", "snippet": "…radio…"},
                                    {"kind": "claude", "id": "old2", "title": "x", "snippet": "…radio…"}])
    assert store.live()[0].snippet == "…radio…"                       # live hit marked on Live
    assert [s.resume_id for s in store.recent()] == ["old2", "old1"]  # ended hit first in Recent
    store.set_search("a", "stale query", [])                          # late reply ignored
    assert store.search["a"]


def test_codex_usage(store):
    store.codex_limits = {"a": {"primary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1791584266},
                                "secondary": {"used_percent": 42.4, "window_minutes": 300}, "ts": "2"}}
    lines = store.codex_usage()
    assert lines[0].startswith("Codex weekly: 10% used · resets ")
    assert lines[1] == "Codex 5-hour: 42% used"


def test_recent_dirs_per_host(store):
    store.ingest("a", {"sessions": [], "bare": [], "home": "/home/x", "boot": 1, "history": [
        {"kind": "claude", "id": "1", "title": "t", "cwd": "/home/x/old", "mtime": 100},
        {"kind": "claude", "id": "2", "title": "t", "cwd": "/home/x/new", "mtime": 200},
        {"kind": "claude", "id": "3", "title": "t", "cwd": "/home/x/new", "mtime": 50},
    ]})
    assert store.recent_dirs("a") == ["~/new", "~/old"]
    assert store.recent_dirs("b") == ["~"]


def test_prefs_sync(store):
    store.prefs_host = "b"
    sent = []
    store.on_prefs_change = lambda add, rm: sent.append((add, rm))
    store.state["pinned"] = ["local-pin"]
    # first shared snapshot: our earlier pins are merged in and pushed up
    store.ingest("b", {"sessions": [], "bare": [], "home": "", "boot": 1, "prefs": {"pinned": ["remote"], "hidden": [], "named": []}})
    assert store.state["pinned"] == ["remote", "local-pin"]
    assert sent == [({"pinned": ["local-pin"], "hidden": [], "named": []}, {})]
    # later snapshots are authoritative
    store.ingest("b", {"sessions": [], "bare": [], "home": "", "boot": 1, "prefs": {"pinned": [], "hidden": ["h"], "named": []}})
    assert store.state["pinned"] == [] and store.state["hidden"] == ["h"]
    # a toggle here is pushed to the prefs host
    s = Session(key="a/tmux/$1", host="a", kind="tmux", agent="claude", name="x", resume_id="r")
    store.toggle("pinned", s)
    assert sent[-1] == ({"pinned": ["a/claude/r"]}, {})
