"""termwall command line. Deliberately imports no Qt on the toggle path so the
hotkey -> overlay round trip stays fast."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time

from .config import CONFIG_PATH, SOCKET_PATH

USAGE = """\
usage: termwall [command]

  (none)          start the daemon if needed and show the overlay
  toggle          show/hide the overlay (bind this to a hotkey)
  daemon          run the daemon in the foreground (no overlay)
  status          host connection status
  quit            stop the daemon
  restart         restart the daemon (after editing the config)
  install         GNOME hotkey (Ctrl+Alt+Space) + systemd user service (starts at login, restarts on crash)
  logs            follow the daemon log (journalctl)
  web             run the phone app server (normally via the termwall-web service)
  install-web     phone app service (systemd / macOS launchd, localhost only) + how to publish it
                  macOS also: tmux from the login session (keychain works); --tailscale-userspace
                  adds Homebrew tailscaled as a no-sudo login agent
  web-pair        print the pairing link + QR code for your phone
  install-shell   on every host: claude/codex start inside tmux (+ ~/.tmux.conf if none)
  uninstall       remove the hotkey + autostart
  screenshot F    save the overlay to F.png
  config          print the config path
"""

HOTKEY = "<Control><Alt>space"
KB_ROOT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
KB_PATH = f"{KB_ROOT}/termwall/"
AUTOSTART = os.path.expanduser("~/.config/autostart/termwall.desktop")


def send(cmd: str, timeout=3.0) -> str | None:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(SOCKET_PATH))
        s.sendall(cmd.encode() + b"\n")
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        s.close()
        return out.decode().strip()
    except OSError:
        return None


def clean_env() -> dict:
    from .agent import leaked_agent_var
    return {k: v for k, v in os.environ.items() if not leaked_agent_var(k)}


SERVICE = os.path.expanduser("~/.config/systemd/user/termwall.service")


def systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def service_installed() -> bool:
    return os.path.exists(SERVICE)


def service_unit(exe: str) -> str:
    return f"""[Unit]
Description=termwall: hotkey overlay for Claude Code / Codex sessions
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={exe} daemon
Restart=on-failure
RestartSec=2
# kitty, tmux, uv live in ~/.local/bin; keep Qt's noisy geometry warnings out of the journal
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=QT_LOGGING_RULES=qt.qpa.*=false

[Install]
WantedBy=graphical-session.target
"""


def spawn_daemon(show: bool):
    if service_installed():
        systemctl("start", "termwall")
        if show:
            for _ in range(50):
                if send("show", timeout=0.3) is not None:
                    return
                time.sleep(0.1)
        return
    exe = shutil.which("termwall") or sys.argv[0]
    args = [exe, "daemon"] + (["--show"] if show else [])
    log = open(os.path.expanduser("~/.cache/termwall/daemon.log"), "ab")
    subprocess.Popen(args, start_new_session=True, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     env=clean_env())


def self_path() -> str:
    return shutil.which("termwall") or os.path.abspath(sys.argv[0])


def gs(*args) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True).stdout.strip()


APPS = os.path.expanduser("~/.local/share/applications/termwall.desktop")
ICON_DIR = os.path.expanduser("~/.local/share/icons/hicolor")


def install_icon(exe: str):
    """Icon + .desktop entry. StartupWMClass makes GNOME show the icon for the
    termwall kitty window (class termwall-term) instead of a generic gear."""
    import shutil as sh
    from pathlib import Path
    assets = Path(__file__).parent / "assets"
    for name in ("termwall", "termwall-tray"):
        dst = Path(ICON_DIR, "scalable", "apps", f"{name}.svg")
        dst.parent.mkdir(parents=True, exist_ok=True)
        sh.copyfile(assets / f"{name}.svg", dst)
    os.makedirs(os.path.dirname(APPS), exist_ok=True)
    with open(APPS, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=termwall\n"
                "Comment=Every Claude Code / Codex session, one hotkey away\n"
                f"Exec={exe}\nIcon=termwall\nTerminal=false\nCategories=Development;Utility;\n"
                "StartupWMClass=termwall-term\nKeywords=claude;codex;tmux;sessions;\n")
    subprocess.run(["gtk-update-icon-cache", "-f", "-t", ICON_DIR], capture_output=True)
    subprocess.run(["update-desktop-database", os.path.dirname(APPS)], capture_output=True)
    print(f"icon + launcher {APPS}")


def install():
    exe = self_path()
    install_icon(exe)
    cur = gs("get", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings")
    paths = [] if cur.startswith("@as") else eval(cur)  # gsettings prints a python-ish list
    if KB_PATH not in paths:
        paths.append(KB_PATH)
        gs("set", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings", str(paths))
    schema = f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{KB_PATH}"
    gs("set", schema, "name", "termwall")
    gs("set", schema, "command", f"{exe} toggle")
    gs("set", schema, "binding", HOTKEY)
    # systemd user service instead of an autostart entry: restarted if it ever crashes
    if os.path.exists(AUTOSTART):
        os.remove(AUTOSTART)
    os.makedirs(os.path.dirname(SERVICE), exist_ok=True)
    with open(SERVICE, "w") as f:
        f.write(service_unit(exe))
    systemctl("daemon-reload")
    if send("status", timeout=0.5) is not None and systemctl("is-active", "termwall").stdout.strip() != "active":
        send("quit")  # hand over from a manually started daemon
        time.sleep(1.0)
    systemctl("enable", "--now", "termwall")
    print(f"hotkey {HOTKEY} -> {exe} toggle\nservice {SERVICE} ({systemctl('is-active', 'termwall').stdout.strip()})")


def uninstall():
    cur = gs("get", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings")
    paths = [] if cur.startswith("@as") else eval(cur)
    if KB_PATH in paths:
        paths.remove(KB_PATH)
        gs("set", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings", str(paths))
    if service_installed():
        systemctl("disable", "--now", "termwall")
    for f in (AUTOSTART, APPS, SERVICE):
        if os.path.exists(f):
            os.remove(f)
    systemctl("daemon-reload")
    print("removed hotkey, service, launcher")


WEB_SERVICE = os.path.expanduser("~/.config/systemd/user/termwall-web.service")


LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/local.termwall.web.plist")


def install_web(args=()):
    from .config import load
    exe = self_path()
    w = load().web
    if sys.platform == "darwin":
        install_tmux_gui_launchd()
        if "--tailscale-userspace" in args:
            install_tailscale_userspace()
        install_web_launchd(exe)
    else:
        os.makedirs(os.path.dirname(WEB_SERVICE), exist_ok=True)
        with open(WEB_SERVICE, "w") as f:
            f.write(f"""[Unit]
Description=termwall web: the phone app (PWA) + terminal bridge (localhost only)
After=network-online.target

[Service]
ExecStart={exe} web
Restart=on-failure
RestartSec=2
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
""")
        systemctl("daemon-reload")
        systemctl("enable", "--now", "termwall-web")
        print(f"service {WEB_SERVICE} ({systemctl('is-active', 'termwall-web').stdout.strip()})")
    print(f"listening on http://{w.get('bind')}:{w.get('port')}")
    print("publish to your tailnet (HTTPS, tailnet-only):\n"
          f"  tailscale serve --bg --https=443 http://127.0.0.1:{w.get('port')}\n"
          "then: termwall web-pair")


def _launchd_load(label: str, plist: str, restart: bool):
    uid = os.getuid()
    loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True).returncode == 0
    if loaded and not restart:
        return "already running"
    if loaded:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        # bootout returns before the job is gone; bootstrapping too early fails with error 5
        for _ in range(50):
            if subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True).returncode != 0:
                break
            time.sleep(0.1)
    for attempt in range(5):
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True, text=True)
        if r.returncode == 0:
            return "loaded"
        time.sleep(0.5)
    return f"failed: {r.stderr.strip()}"


def _write_plist(label: str, args: list[str], keepalive: bool, log: str | None = None) -> str:
    home = os.path.expanduser("~")
    path = os.path.join(home, "Library", "LaunchAgents", f"{label}.plist")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr = "".join(f"<string>{a}</string>" for a in args)
    logs = (f"<key>StandardOutPath</key><string>{log}</string>"
            f"<key>StandardErrorPath</key><string>{log}</string>") if log else ""
    with open(path, "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{arr}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>{"<true/>" if keepalive else "<false/>"}
  {logs}
</dict></plist>
""")
    return path


def install_tmux_gui_launchd():
    """macOS: start tmux from the GUI login session and keep it alive with no sessions.
    A tmux server first started over ssh lives in that ssh security session: panes
    can't read the login keychain, so Claude shows "Not logged in"."""
    conf = os.path.expanduser("~/.tmux.conf")
    text = open(conf).read() if os.path.exists(conf) else ""
    if "exit-empty" not in text:
        with open(conf, "a") as f:
            f.write("\n# termwall: keep the server (and its GUI login-session context) alive with no sessions\n"
                    "set -g exit-empty off\n")
    tm = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    plist = _write_plist("local.termwall.tmux", [tm, "start-server"], keepalive=False)
    print(f"tmux login agent: {_launchd_load('local.termwall.tmux', plist, restart=False)}")
    ctx = subprocess.run([tm, "show-environment", "-g", "SSH_CONNECTION"], capture_output=True, text=True)
    if ctx.returncode == 0 and ctx.stdout.startswith("SSH_CONNECTION="):
        print("  ! the running tmux server was started over ssh (no keychain). When convenient:\n"
              "    tmux kill-server && launchctl kickstart gui/$(id -u)/local.termwall.tmux   (closes its sessions)")


def install_tailscale_userspace():
    """macOS without the Tailscale app: Homebrew tailscaled as a per-user LaunchAgent in
    userspace-networking mode (no sudo, no system extension) + a `tailscale` wrapper."""
    tsd = "/opt/homebrew/bin/tailscaled"
    if not os.path.exists(tsd):
        print("tailscale: run `brew install tailscale` first")
        return
    d = os.path.expanduser("~/.local/share/tailscale")
    os.makedirs(d, exist_ok=True)
    plist = _write_plist("local.termwall.tailscaled", [tsd, "--tun=userspace-networking", f"--socket={d}/tailscaled.sock",
                                                  f"--statedir={d}"], keepalive=True,
                         log=os.path.expanduser("~/Library/Logs/tailscaled.log"))
    wrapper = os.path.expanduser("~/.local/bin/tailscale")
    os.makedirs(os.path.dirname(wrapper), exist_ok=True)
    with open(wrapper, "w") as f:
        f.write(f"#!/bin/sh\n# per-user tailscaled (userspace networking, LaunchAgent local.termwall.tailscaled)\n"
                f"exec /opt/homebrew/bin/tailscale --socket={d}/tailscaled.sock \"$@\"\n")
    os.chmod(wrapper, 0o755)
    print(f"tailscaled login agent: {_launchd_load('local.termwall.tailscaled', plist, restart=False)}")
    print("  first time: ~/.local/bin/tailscale up --hostname=$(hostname -s)   (open the login link)")


def install_web_launchd(exe: str):
    """macOS: a LaunchAgent that starts at login and is restarted if it dies."""
    plist = _write_plist("local.termwall.web", [exe, "web"], keepalive=True,
                         log=os.path.expanduser("~/Library/Logs/termwall-web.log"))
    print(f"termwall-web login agent: {_launchd_load('local.termwall.web', plist, restart=True)}")


def web_pair(base: str | None):
    """Show the app URL (QR) + a 6-digit code the installed app exchanges for the token."""
    import json as _json
    import secrets as _secrets
    from .webserver import PAIR_PATH, token
    token()  # make sure one exists
    if not base:
        base = tailscale_url() or "http://127.0.0.1:8770"
    code = f"{_secrets.randbelow(10 ** 6):06d}"
    PAIR_PATH.write_text(_json.dumps({"code": code, "expires": time.time() + 600, "tries": 0}))
    PAIR_PATH.chmod(0o600)
    url = base.rstrip("/") + "/"
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)
    except Exception:
        pass
    print(url)
    print("\n  1. open that on the phone (Safari or Chrome) -> Share -> Add to Home Screen")
    print("  2. open termwall from the home screen and enter this code:\n")
    print(f"        {code[:3]} {code[3:]}        (valid 10 minutes)\n")


def tailscale_url() -> str | None:
    import json as _json
    try:
        ts = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=5)
        name = _json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
        return f"https://{name}" if name else None
    except Exception:
        return None


def install_shell():
    from pathlib import Path
    from .config import load
    from .hosts import ssh_base
    here = Path(__file__).parent / "shell"
    hook, tconf = (here / "termwall.sh").read_text(), (here / "tmux.conf").read_text()
    script = f"""set -e
mkdir -p ~/.config/termwall
cat > ~/.config/termwall/termwall.sh <<'TWEOF'
{hook}TWEOF
[ -e ~/.tmux.conf ] || {{ cat > ~/.tmux.conf <<'TWEOF'
{tconf}TWEOF
echo "  wrote ~/.tmux.conf"; }}
case "$SHELL" in *zsh) rc=~/.zshrc ;; *) rc=~/.bashrc ;; esac
if ! grep -q 'termwall/termwall.sh' "$rc" 2>/dev/null; then
  printf '\n# termwall: interactive claude/codex run inside tmux (see ~/.config/termwall/termwall.sh)\n[ -f ~/.config/termwall/termwall.sh ] && . ~/.config/termwall/termwall.sh\n' >> "$rc"
  echo "  hooked $rc"
else
  echo "  $rc already hooked"
fi
"""
    for h in load().hosts:
        print(f"{h.name}:")
        args = (ssh_base(h.ssh) + ["sh", "-s"]) if h.ssh else ["sh", "-s"]
        r = subprocess.run(args, input=script, text=True, capture_output=True)
        print(r.stdout.rstrip() or "", r.stderr.rstrip() if r.returncode else "")


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""
    os.makedirs(os.path.expanduser("~/.cache/termwall"), exist_ok=True)
    if cmd in ("", "show", "toggle"):
        if send(cmd or "show") is None:
            spawn_daemon(show=True)
    elif cmd == "daemon":
        if send("status", timeout=1) is not None:
            print("termwall daemon already running")
            return
        from .app import run
        run(show="--show" in argv)
    elif cmd == "debug":
        print(send("debug"))
    elif cmd == "key":  # test hook (not in USAGE): synthesize keys into the overlay
        print(send("key " + " ".join(argv[1:])) or "termwall daemon not running")
    elif cmd in ("status", "quit", "hide"):
        r = send(cmd)
        print(r if r is not None else "termwall daemon not running")
    elif cmd == "restart" and service_installed():
        r = systemctl("restart", "termwall")
        print("restarted" if r.returncode == 0 else r.stderr.strip())
    elif cmd == "restart":
        send("quit")
        for _ in range(50):
            if send("status", timeout=0.2) is None:
                break
            time.sleep(0.1)
        spawn_daemon(show=False)
        print("restarted")
    elif cmd == "screenshot":
        path = os.path.abspath(argv[1] if len(argv) > 1 else "termwall.png")
        r = send(f"screenshot {path}", timeout=10)
        print(path if r == "ok" else (r or "termwall daemon not running"))
    elif cmd == "install":
        install()
    elif cmd == "web":
        from .webserver import run
        run()
    elif cmd == "install-web":
        install_web(argv[1:])
    elif cmd == "web-pair":
        web_pair(argv[1] if len(argv) > 1 else None)
    elif cmd == "logs":
        os.execvp("journalctl", ["journalctl", "--user", "-u", "termwall", "-f", "-n", "50"])
    elif cmd == "install-shell":
        install_shell()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "config":
        print(CONFIG_PATH)
    else:
        print(USAGE)
