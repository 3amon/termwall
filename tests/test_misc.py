import datetime as dt
import tomllib

from termwall import config, presence, render
from termwall.overlay import slug


def test_quiet_hours():
    d = lambda h, m=0: dt.datetime(2026, 10, 3, h, m)  # noqa: E731
    assert [presence.in_quiet_hours("23:00-08:00", d(h)) for h in (22, 23, 2, 7, 8, 12)] == \
        [False, True, True, True, False, False]
    assert presence.in_quiet_hours("13:00-14:00", d(13, 30))
    assert not presence.in_quiet_hours("", d(1))
    assert not presence.in_quiet_hours("garbage", d(1))


def test_set_option_keeps_comments(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(config.DEFAULT_CONFIG)
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    config.set_option("ui", "order", "stable")
    config.set_option("notify", "ntfy_topic", "t-1")
    config.set_option("brand_new", "x", True)
    d = tomllib.loads(p.read_text())
    assert d["ui"]["order"] == "stable" and d["notify"]["ntfy_topic"] == "t-1" and d["brand_new"]["x"] is True
    assert "# termwall config" in p.read_text()
    assert len(d["hosts"]) == 2


def test_default_config_parses():
    d = tomllib.loads(config.DEFAULT_CONFIG)
    assert {"agents", "terminal", "ui", "notify", "hosts"} <= set(d)


def test_sgr_parse():
    lines = render.parse("plain \x1b[1;31mred\x1b[0m \x1b[38;2;10;20;30mrgb\x1b[0m\n\x1b[7mrev\x1b[m\n\n")
    assert len(lines) == 2                         # trailing blank lines dropped
    texts = [t for t, _ in lines[0]]
    assert texts == ["plain ", "red", " ", "rgb"]
    _, (fg, bg, bold, *_rest) = lines[0][1]
    assert bold and fg is not None
    assert lines[1][0][1][4] is True               # reverse video


def test_slug():
    assert slug("Fix flaky JLCPCB test!", 4) == "fix-flaky-jlcpcb-test"
    assert slug("", 3) == "session"
