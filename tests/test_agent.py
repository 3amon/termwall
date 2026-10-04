import json
import os

from conftest import CLAUDE_ROWS, CODEX_ROWS, write_jsonl
from termwall import agent

SID = "11111111-2222-3333-4444-555555555555"


def claude_file(home, sid=SID, rows=CLAUDE_ROWS):
    return write_jsonl(home / ".claude" / "projects" / "-work-proj" / f"{sid}.jsonl", rows)


def codex_file(home):
    return write_jsonl(home / ".codex" / "sessions" / "2026" / "10" / "03" /
                       "rollout-2026-10-03T10-00-00-01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000.jsonl", CODEX_ROWS)


def test_parse_claude(home):
    t = agent.parse_claude(str(claude_file(home)))
    assert t["title"] == "Fix flaky JLCPCB test"
    assert t["has_title"] is True
    assert t["cwd"] == "/work/proj"          # where it started (project dir), not the later cd
    assert t["last_user"] == "fix the flaky test please"   # tool results / meta skipped
    assert t["last_assistant"] == "Fixed: the retry was racing."   # sidechain skipped
    assert (t["model"], t["effort"]) == ("claude-opus-5-5", "high")
    assert t["ctx"] == 5 + 500 + 120000
    assert t["turn"] == "done"


def test_parse_claude_empty_session_is_ignored(home):
    p = claude_file(home, rows=[{"type": "mode", "mode": "normal"}])
    assert agent.parse_claude(str(p)) is None


def test_parse_codex(home):
    t = agent.parse_codex(str(codex_file(home)))
    assert t["id"] == "01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
    assert t["last_user"] == "which meshtastic radio?"     # AGENTS.md preamble skipped
    assert t["last_assistant"] == "The WisMesh Tag."
    assert (t["model"], t["effort"]) == ("gpt-6-astra", "xhigh")
    assert (t["ctx"], t["ctx_window"]) == (51680, 258400)
    assert t["limits"]["primary"]["used_percent"] == 10.0
    assert t["turn"] == "idle"


def test_transcript_and_search(home):
    claude_file(home)
    codex_file(home)
    msgs = agent.cmd_transcript({"kind": "claude", "id": SID})["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert agent.cmd_transcript({"kind": "codex", "id": "01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000"})["ok"]

    hits = agent.cmd_search({"q": "WisMesh"})["results"]
    assert [h["kind"] for h in hits] == ["codex"] and "WisMesh" in hits[0]["snippet"]
    assert agent.cmd_search({"q": "racing"})["results"][0]["id"] == SID
    assert agent.cmd_search({"q": "ignored"})["results"] == []      # tool output isn't searchable
    assert agent.cmd_search({"q": "racing", "skip": ["-work-proj"]})["results"] == []
    assert agent.cmd_search({"q": "ab"})["results"] == []           # too short


def test_git_info(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    (repo / "src").mkdir()
    g = agent.git_info(str(repo / "src"))
    assert (g["repo"], g["branch"]) == ("myrepo", "feature/x")
    # a worktree points at the main repo's gitdir
    wt = tmp_path / "wt"
    wt.mkdir()
    gd = repo / ".git" / "worktrees" / "wt"
    gd.mkdir(parents=True)
    (gd / "HEAD").write_text("ref: refs/heads/other\n")
    (gd / "commondir").write_text("../..\n")
    (wt / ".git").write_text(f"gitdir: {gd}\n")
    g2 = agent.git_info(str(wt))
    assert g2["branch"] == "other" and g2["common"] == g["common"]
    assert agent.git_info(str(tmp_path / "nowhere")) is None


def test_leaked_agent_env_vars():
    assert agent.leaked_agent_var("CLAUDECODE")
    assert agent.leaked_agent_var("CLAUDE_CODE_SESSION_ID")
    assert not agent.leaked_agent_var("CLAUDE_CONFIG_DIR")
    assert not agent.leaked_agent_var("PATH")


def test_boot_time_is_sane():
    import time
    assert 0 < agent.boot_time() <= time.time()


def test_codex_id_from_command_line(home):
    codex_file(home)
    procs = {4242: (1, "pts/1", "codex resume 01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000 --some-flag")}
    info = agent.Agent().codex_info(4242, procs)
    assert info["id"] == "01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
    assert info["title"] == "which meshtastic radio?"


def test_prefs_roundtrip(home, monkeypatch):
    monkeypatch.setattr(agent, "PREFS_PATH", str(home / ".config" / "termwall" / "prefs.json"))
    assert agent.read_prefs() == {"pinned": [], "hidden": [], "named": []}
    agent.cmd_prefs_set({"add": {"pinned": ["a/claude/1", "a/claude/2"], "bogus": ["x"]}})
    agent.cmd_prefs_set({"add": {"pinned": ["a/claude/1"]}, "remove": {"pinned": ["a/claude/2"]}})
    assert agent.read_prefs()["pinned"] == ["a/claude/1"]
    a = agent.Agent()
    assert a.prefs_if_changed(full=True)["pinned"] == ["a/claude/1"]
    assert a.prefs_if_changed(full=False) is None          # unchanged -> not resent
