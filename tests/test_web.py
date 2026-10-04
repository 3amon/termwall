import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from termwall import config, model, webserver


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(model, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(webserver, "TOKEN_PATH", tmp_path / "web_token")

    async def no_hosts(self):          # don't connect to real machines in tests
        await asyncio.sleep(3600)
    monkeypatch.setattr(webserver.Host, "run", no_hosts)
    return webserver.make_app()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_token_required_everywhere(app):
    async def go():
        async with TestClient(TestServer(app)) as c:
            tok = app["token"]
            assert (await c.get("/")).status == 200                      # app shell is public
            assert (await c.get("/manifest.webmanifest")).status == 200
            assert (await c.get("/xterm.js")).status == 200
            assert (await c.get("/apple-touch-icon.png")).status == 200
            for path in ("/api/state", "/api/peek?key=x", "/api/search?q=abc", "/ws/state", "/ws/term?key=x"):
                assert (await c.get(path)).status == 401, path
                assert (await c.get(path, headers={"X-Termwall-Token": "wrong"})).status == 401, path
            assert (await c.post("/api/reply", json={"key": "x"})).status == 401
            r = await c.get("/api/state", headers={"X-Termwall-Token": tok})
            assert r.status == 200 and {"hosts", "live", "recent", "usage"} <= set(await r.json())
            r = await c.post("/api/reply", json={"key": "nope", "text": "hi"}, headers={"X-Termwall-Token": tok})
            assert r.status == 404
            assert (await c.get("/../config.toml")).status in (400, 404)    # no path traversal
    run(go())


def test_token_file_is_private(app):
    import stat
    assert stat.S_IMODE(webserver.TOKEN_PATH.stat().st_mode) == 0o600
    assert len(app["token"]) >= 30


def test_session_json(app):
    st = app["store"]
    s = model.Session(key="workstation/tmux/$1", host="workstation", kind="tmux", agent="claude", name="w",
                      title="Fix it", status="approval", screen="a\n\x1b[1mDo you want to proceed?\x1b[0m\n\n\n",
                      model="claude-opus-5-5", effort="high", ctx=250_000)
    j = webserver.session_json(st, s)
    assert j["status"] == "approval" and j["model"] == "Opus 5.5 · high"
    assert j["ctx"] == 0.25 and j["screen"].endswith("\x1b[0m")       # trailing blank lines trimmed


def test_resume_opens_live_twin(app):
    st = app["store"]
    st.host_state["workstation"] = "online"
    st.by_host["workstation"] = {"workstation/tmux/$5": model.Session(
        key="workstation/tmux/$5", host="workstation", kind="tmux", agent="codex", name="api", resume_id="abc")}
    hist = model.Session(key="workstation/history/codex/abc", host="workstation", kind="history", agent="codex", resume_id="abc")
    assert webserver.live_twin(st, hist).key == "workstation/tmux/$5"
    other = model.Session(key="k", host="workstation", kind="history", agent="codex", resume_id="zzz")
    assert webserver.live_twin(st, other) is None


def test_every_app_asset_is_in_the_repo(app):
    """The service worker refuses to install if a cached asset 404s (that broke push)."""
    import json as _json
    import re as _re
    import subprocess
    tracked = set(subprocess.run(["git", "ls-files", "src/termwall/web"], capture_output=True, text=True,
                                 cwd=webserver.WEB_DIR.parents[2]).stdout.split())
    names = {p.rsplit("/", 1)[-1] for p in tracked}
    if not names:   # not a git checkout (unpacked source archive): check the files on disk
        names = {p.name for p in webserver.WEB_DIR.rglob("*") if p.is_file()}
    sw = (webserver.WEB_DIR / "sw.js").read_text()
    shell = _re.search(r"const SHELL = \[(.*?)\];", sw, _re.S).group(1)
    wanted = [u.strip().strip('"') for u in shell.split(",") if u.strip() and u.strip() != '"./"']
    manifest = _json.loads((webserver.WEB_DIR / "manifest.webmanifest").read_text())
    wanted += [i["src"] for i in manifest["icons"]]
    html = (webserver.WEB_DIR / "index.html").read_text()
    wanted += _re.findall(r'(?:href|src)="([\w.-]+\.(?:png|js|css|webmanifest))"', html)
    missing = sorted({w for w in wanted if w not in names})
    assert not missing, f"not committed: {missing}"

    async def go():
        async with TestClient(TestServer(app)) as c:
            for w in set(wanted):
                assert (await c.get("/" + w)).status == 200, w
    run(go())


def test_vapid_subject_is_acceptable_to_apple():
    """Apple returns 403 BadJwtToken for localhost subjects; py_vapid wants mailto: or a bare https origin."""
    import re as _re
    from termwall import webpush
    sub = webpush.VAPID_SUBJECT
    assert "localhost" not in sub
    assert _re.fullmatch(r"mailto:[^@\s]+@[\w-]+(\.[\w-]+)+|https://[\w-]+(\.[\w-]+)+", sub), sub
