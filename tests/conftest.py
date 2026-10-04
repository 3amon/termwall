import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# termwall.config reads XDG_CONFIG_HOME at import time. Keep the config, pairing token and
# push key paths of the code under test out of the real ~/.config/termwall.
os.environ["XDG_CONFIG_HOME"] = __import__("tempfile").mkdtemp(prefix="termwall-test-config-")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake $HOME with Claude + Codex data, wired into the agent."""
    from termwall import agent
    monkeypatch.setattr(agent, "HOME", str(tmp_path))
    agent._cache.clear()
    return tmp_path


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


CLAUDE_ROWS = [
    {"type": "user", "cwd": "/work/proj", "message": {"role": "user", "content": "fix the flaky test please"}},
    {"type": "assistant", "cwd": "/work/proj", "effort": "high",
     "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Looking at it now."}],
                 "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 99000}}},
    {"type": "ai-title", "aiTitle": "Fix flaky JLCPCB test"},
    {"type": "user", "cwd": "/work/proj/sub", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "ignored"}]}},
    {"type": "user", "cwd": "/work/proj/sub", "isMeta": True, "message": {"role": "user", "content": "<meta>"}},
    {"type": "assistant", "isSidechain": True,
     "message": {"model": "claude-haiku-4-5-20251001", "content": [{"type": "text", "text": "subagent chatter"}]}},
    {"type": "assistant", "cwd": "/work/proj/sub", "effort": "high",
     "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Fixed: the retry was racing."}],
                 "usage": {"input_tokens": 5, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 120000}}},
    {"type": "system", "subtype": "turn_duration"},
]

CODEX_ROWS = [
    {"type": "session_meta", "payload": {"id": "01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000", "cwd": "/work/codexproj"}},
    {"type": "turn_context", "payload": {"cwd": "/work/codexproj", "model": "gpt-6-astra", "effort": "xhigh"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
    {"type": "event_msg", "payload": {"type": "task_started"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "which meshtastic radio?"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": "The WisMesh Tag."}]}},
    {"type": "event_msg", "timestamp": "2026-10-03T18:46:23Z", "payload": {
        "type": "token_count",
        "info": {"last_token_usage": {"input_tokens": 51680}, "model_context_window": 258400},
        "rate_limits": {"primary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1791584266},
                        "secondary": None}}},
    {"type": "event_msg", "payload": {"type": "task_complete"}},
]
