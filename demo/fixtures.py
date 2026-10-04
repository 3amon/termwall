"""Invented sessions for screenshots and manual UI checks.

Nothing here comes from a real machine. The dictionaries have the same shape as
the snapshots `termwall/agent.py` prints (one per host), so they go through the
normal `Store.ingest` path: status detection, approval-prompt matching, context
meter, MRU ordering and the "waiting for you" transition are all the real code.

Hosts: "workstation" (the machine the overlay runs on) and "buildbox" (a remote
reached over ssh in a real setup; here nothing is contacted).
"""
from __future__ import annotations

import hashlib
import time

HOSTS = ["workstation", "buildbox"]
HOME = "/home/dev"

DIM, BOLD, RST = "\x1b[2m", "\x1b[1m", "\x1b[0m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\x1b[3{n}m" for n in (1, 2, 3, 4, 5, 6))
ORANGE = "\x1b[38;5;209m"
RULE = DIM + "─" * 80 + RST


def _claude_footer(hint="? for shortcuts"):
    return f"\n{RULE}\n{BLUE}❯{RST} \n{RULE}\n  {DIM}{hint}{RST}"


SCREEN_PAYMENTS = f"""\
{BLUE}❯{RST} the webhook handler double-charges when the provider retries. fix it

{GREEN}●{RST} I'll look at how the handler derives its idempotency key.

{GREEN}●{RST} {BOLD}Read{RST}(src/webhooks/handler.py)
  {DIM}⎿  Read 212 lines{RST}

{GREEN}●{RST} The key is built from the request id, which is new on every retry, so
  the dedupe table never matches. Switching it to the provider's event id.

{GREEN}●{RST} {BOLD}Update{RST}(src/webhooks/handler.py)
  {DIM}⎿  Updated with 6 additions and 3 removals{RST}
       {RED}41 -    key = f"charge:{{request.id}}"{RST}
       {GREEN}41 +    key = f"charge:{{event['id']}}"{RST}
       {GREEN}42 +    if store.seen(key):{RST}
       {GREEN}43 +        return Response(status=200)   # replay: do nothing{RST}

{GREEN}●{RST} {BOLD}Bash{RST}(pytest tests/webhooks -q)
  {DIM}⎿  Running…{RST}

{ORANGE}✻{RST} Running the webhook tests… {DIM}(38s · ↓ 2.1k tokens · esc to interrupt){RST}
{_claude_footer()}"""

SCREEN_FIRMWARE = f"""\
{BLUE}❯{RST} flash the test image and check the SPI flash on the rev B board

{GREEN}●{RST} {BOLD}Bash{RST}(west build -b custom_revb app -p auto)
  {DIM}⎿  [142/142] Linking C executable zephyr/zephyr.elf{RST}
     {DIM}Memory region    Used Size  Region Size  %age Used{RST}
     {DIM}       FLASH:     96412 B      512 KB     18.39%{RST}
     {DIM}         RAM:     21904 B       64 KB     33.42%{RST}

{GREEN}●{RST} The build is clean. The next step writes the image to the board.

{RULE}
 {BOLD}Bash command{RST}

   west flash --runner jlink --hex-file build/zephyr/zephyr.hex
   {DIM}Write the test image to the board{RST}

 Do you want to proceed?
 {BLUE}❯ 1. Yes{RST}
   2. Yes, and don't ask again for west commands in this project
   3. No, and tell Claude what to do differently {DIM}(esc){RST}"""

SCREEN_DOCS = f"""\
{DIM}›{RST} move the how-to pages under guides/ and fix the sidebar

{CYAN}•{RST} Moved 14 pages under guides/ and regenerated the sidebar.

{CYAN}•{RST} {BOLD}Ran{RST} npm run build
  {DIM}└ ✓ 212 pages built in 9.4s{RST}
    {DIM}  2 warnings: redirect target missing (old/quickstart, old/install){RST}

{CYAN}•{RST} Build passes. Two redirects still point at the old paths; I listed
  them in MIGRATION.md rather than guessing where they should go.

{DIM}• Worked for 3m 12s{RST}

{DIM}›{RST} Ask Codex to do anything
  {DIM}GPT-6 Astra xhigh · ~/src/docs-site{RST}"""

SCREEN_INDEXER = f"""\
{DIM}›{RST} the nightly reindex takes 50 minutes. profile it and cut the waste

{CYAN}•{RST} {BOLD}Ran{RST} python -m cProfile -o reindex.prof tools/reindex.py -n 5000
  {DIM}└ 5000 documents in 71.3s{RST}

{CYAN}•{RST} 83% of the time is in tokenize(): the stop-word list is rebuilt for
  every document. Hoisting it to module scope and batching the writes.

{CYAN}•{RST} {BOLD}Edited{RST} indexer/tokenize.py {GREEN}(+9{RST} {RED}-14){RST}
{CYAN}•{RST} {BOLD}Edited{RST} indexer/writer.py {GREEN}(+21{RST} {RED}-6){RST}

{CYAN}•{RST} {BOLD}Running{RST} python tools/reindex.py -n 5000
  {DIM}└ 3200/5000 documents…{RST}

{MAGENTA}◦{RST} Working {DIM}(1m 04s · esc to interrupt){RST}

{DIM}›{RST} Ask Codex to do anything
  {DIM}GPT-6 Astra high · ~/src/search-indexer{RST}"""

SCREEN_LOADTEST = f"""\
{BLUE}❯{RST} summarise last week's load test

{GREEN}●{RST} p95 latency held at 180 ms up to 400 rps and climbed to 610 ms at
  600 rps. The connection pool (size 20) is the first thing to saturate.
  Raw numbers are in reports/load-test.md.
{_claude_footer()}"""


def _h(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _tmux(sid, name, cwd, screen, age, agent=None, git=None, attached=0, cmd=None, created_age=7200, now=0.0):
    return {
        "id": sid, "name": name, "created": int(now - created_age), "activity": int(now - age),
        "attached": attached, "windows": 1,
        "pane": {"cmd": cmd or (agent or {}).get("kind") or "bash", "cwd": cwd, "w": 84, "h": 26},
        "agent": agent, "screen": screen, "screen_hash": _h(screen), "git": git,
    }


def _git(repo, branch):
    return {"repo": repo, "branch": branch, "common": f"{HOME}/src/{repo}/.git"}


def snapshots(now: float | None = None) -> dict[str, list[dict]]:
    """host -> [earlier snapshot, current snapshot].

    Two per host so sessions can make the real busy -> idle transition that marks
    them "waiting for you".
    """
    now = now or time.time()
    day = 86400

    def claude(sid, title, cwd, status, ctx, model="claude-opus-5-5", effort="high", age=30, **kw):
        return {"kind": "claude", "id": sid, "title": title, "has_title": True, "cwd": cwd, "status": status,
                "updated": int(now - age), "model": model, "effort": effort, "ctx": ctx, **kw}

    def codex(sid, title, cwd, status, ctx, effort="xhigh", age=30, **kw):
        return {"kind": "codex", "id": sid, "title": title, "has_title": True, "cwd": cwd, "status": status,
                "updated": int(now - age), "model": "gpt-6-astra", "effort": effort, "ctx": ctx,
                "ctx_window": 258400, **kw}

    def workstation(docs_status):
        return {
            "type": "snapshot", "host": "workstation", "home": HOME, "boot": int(now - 9 * day),
            "platform": "linux", "tmux": "tmux",
            "sessions": [
                _tmux("$1", "payments-api", f"{HOME}/src/payments-api", SCREEN_PAYMENTS, 4, now=now, attached=1,
                      git=_git("payments-api", "fix/idempotency-keys"),
                      agent=claude("a1f0c001", "Fix duplicate webhook charge",
                                   f"{HOME}/src/payments-api", "busy", 430_000, age=4)),
                _tmux("$2", "firmware-bringup", f"{HOME}/src/firmware-bringup", SCREEN_FIRMWARE, 95, now=now,
                      git=_git("firmware-bringup", "revb-spi-flash"),
                      agent=claude("a1f0c002", "SPI flash bring-up, rev B",
                                   f"{HOME}/src/firmware-bringup", "idle", 182_000, effort="medium", age=95)),
                _tmux("$3", "docs-site", f"{HOME}/src/docs-site", SCREEN_DOCS, 260, now=now,
                      git=_git("docs-site", "nav-migration"),
                      agent=codex("0d0c5001-0000-7000-8000-000000000001", "Move how-to pages to guides/",
                                  f"{HOME}/src/docs-site", docs_status, 51_680, age=260)),
            ],
            "bare": [
                {"kind": "claude", "id": "a1f0c005", "pid": 4242, "tty": "pts/4", "entrypoint": "cli",
                 "title": "Backfill the events table", "cwd": f"{HOME}/src/data-pipeline", "status": "idle",
                 "updated": int(now - 2400), "model": "claude-opus-5-5", "effort": "high", "ctx": 640_000,
                 "git": _git("data-pipeline", "main"),
                 "last_user": "backfill events for August in daily batches and stop on the first error",
                 "last_assistant": "Backfilled 31 daily batches (4.2 M rows). No errors; row counts match "
                                   "the source for every day. The script is in tools/backfill_events.py."},
            ],
            "history": [
                {"kind": "claude", "id": "a1f0c0a1", "title": "Rotate the staging database credentials",
                 "cwd": f"{HOME}/src/infra", "mtime": int(now - 5 * 3600), "model": "claude-opus-5-5",
                 "effort": "high", "ctx": 210_000,
                 "last_user": "rotate the staging db credentials and update the deploy secrets",
                 "last_assistant": "Rotated. The old credentials are revoked and the deploy picked up the new "
                                   "secret on the 14:02 rollout."},
                {"kind": "codex", "id": "0d0c5001-0000-7000-8000-0000000000a2",
                 "title": "Upgrade the linter config", "cwd": f"{HOME}/src/docs-site",
                 "mtime": int(now - 26 * 3600), "model": "gpt-6-astra", "effort": "medium", "ctx": 30_000,
                 "ctx_window": 258400, "last_user": "upgrade eslint to the flat config",
                 "last_assistant": "Converted .eslintrc to eslint.config.js; 0 new warnings."},
            ],
            "codex_limits": {"ts": "2", "primary": {"used_percent": 18.0, "window_minutes": 10080,
                                                    "resets_at": int(now + 3 * day)},
                             "secondary": {"used_percent": 42.0, "window_minutes": 300}},
        }

    def buildbox(indexer_status):
        return {
            "type": "snapshot", "host": "buildbox", "home": HOME, "boot": int(now - 30 * day),
            "platform": "linux", "tmux": "tmux",
            "sessions": [
                _tmux("$1", "search-indexer", f"{HOME}/src/search-indexer", SCREEN_INDEXER, 9, now=now,
                      git=_git("search-indexer", "perf/reindex"),
                      agent=codex("0d0c5001-0000-7000-8000-000000000006", "Speed up the nightly reindex",
                                  f"{HOME}/src/search-indexer", indexer_status, 142_000, effort="high", age=9)),
                _tmux("$2", "load-tests", f"{HOME}/src/load-tests", SCREEN_LOADTEST, 9 * day + 3600, now=now,
                      created_age=12 * day, git=_git("load-tests", "main"),
                      agent=claude("a1f0c008", "Load test summary", f"{HOME}/src/load-tests",
                                   "idle", 38_000, model="claude-haiku-4-5-20251001", effort="low",
                                   age=9 * day + 3600)),
            ],
            "bare": [],
            "history": [
                {"kind": "claude", "id": "a1f0c0b1", "title": "Bisect the flaky checkout test",
                 "cwd": f"{HOME}/src/mobile-app", "mtime": int(now - 3 * 3600), "model": "claude-opus-5-5",
                 "effort": "high", "ctx": 320_000,
                 "last_user": "the checkout test fails about one run in ten. bisect it",
                 "last_assistant": "It is a race between the cart refresh and the submit handler, introduced "
                                   "in the debounce change. Fixed by awaiting the refresh; 200 runs green."},
            ],
        }

    return {
        "workstation": [workstation("busy"), workstation("idle")],
        "buildbox": [buildbox("busy"), buildbox("busy")],
    }


def load(store, now: float | None = None) -> None:
    """Feed the fixture snapshots through the store exactly as host connections would."""
    for host, snaps in snapshots(now).items():
        store.host_state[host] = "online"
        for snap in snaps:
            store.ingest(host, snap)
    store.finished, store.attention = [], []          # nothing to notify about in a demo
    store.state["pinned"] = ["workstation/claude/a1f0c001"]


CONFIG_TOML = """\
[[hosts]]
name = "workstation"

[[hosts]]
name = "buildbox"
ssh = "buildbox"

[ui]
tray = false
"""
