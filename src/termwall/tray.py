"""System tray icon: waiting-badge, host status, quick settings."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import config, theme

ASSETS = Path(__file__).parent / "assets"


def app_icon() -> QIcon:
    return QIcon(str(ASSETS / "termwall.svg"))


class Tray(QObject):
    def __init__(self, ctl):
        super().__init__(ctl)
        self.ctl = ctl
        self.renderer = QSvgRenderer(str(ASSETS / "termwall-tray.svg"))
        self.badge = None
        self.tray = QSystemTrayIcon(self._icon(False), self)
        self.tray.activated.connect(self._activated)
        self.menu = QMenu()
        self._build()
        self.tray.setContextMenu(self.menu)
        self.tray.show()
        self.refresh()

    # ---------------------------------------------------------------- icon
    def _icon(self, waiting: bool) -> QIcon:
        icon = QIcon()
        for size in (16, 22, 24, 32, 48, 64):
            pm = QPixmap(size, size)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            self.renderer.render(p, QRectF(0, 0, size, size))
            if waiting:
                r = size * 0.19
                p.setPen(QColor(theme.TERM_BG))
                p.setBrush(QColor(theme.WAITING))
                p.drawEllipse(QRectF(size - 2 * r - 0.5, 0.5, 2 * r, 2 * r))
            p.end()
            icon.addPixmap(pm)
        return icon

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.ctl.overlay.toggle()

    # ---------------------------------------------------------------- menu
    def _build(self):
        m, cfg = self.menu, self.ctl.cfg
        show = m.addAction("Show the wall\tCtrl+Alt+Space")
        show.triggered.connect(self.ctl.overlay.summon)
        keys = m.addAction("Keyboard shortcuts…")
        keys.triggered.connect(self.show_help)
        new = m.addAction("New session…")
        new.triggered.connect(lambda: (self.ctl.overlay.summon(), self.ctl.overlay.open_new_panel()))
        m.addSeparator()
        self.host_actions = {}
        for h in cfg.hosts:
            a = m.addAction(h.name)
            a.setEnabled(False)
            self.host_actions[h.name] = a
        self.usage_actions = [m.addAction("") for _ in range(2)]
        for a in self.usage_actions:
            a.setEnabled(False)
            a.setVisible(False)
        self.stale_action = m.addAction("Close stale sessions…")
        self.stale_action.triggered.connect(self.close_stale)
        self.stale_action.setVisible(False)
        m.addSeparator()

        nm = m.addMenu("Notifications")
        n = cfg.notify
        self.notify = nm.addAction("Desktop notifications")
        self.notify.setCheckable(True)
        self.notify.setChecked(bool(cfg.ui.get("notify", True)))
        self.notify.toggled.connect(lambda v: self._set("ui", "notify", v))
        appr = nm.addAction("⚠ Approval prompts")
        appr.setCheckable(True)
        appr.setChecked(bool(n.get("approvals", True)))
        appr.toggled.connect(lambda v: self._set("notify", "approvals", v))
        nm.addSection("Agent finished its turn")
        g1 = QActionGroup(nm)
        for label, val in (("Always", "always"), ("Only when I'm away", "away"), ("Never", "never")):
            a = nm.addAction(label)
            a.setCheckable(True)
            a.setChecked(n.get("finished", "always") == val)
            a.setActionGroup(g1)
            a.triggered.connect(lambda _=False, v=val: self._set("notify", "finished", v))
        nm.addSection("Phone (ntfy) while away")
        g2 = QActionGroup(nm)
        for label, val in (("Approvals only", "approvals"), ("Approvals + finished", "all"), ("Off", "never")):
            a = nm.addAction(label)
            a.setCheckable(True)
            a.setChecked(n.get("phone", "approvals") == val)
            a.setActionGroup(g2)
            a.triggered.connect(lambda _=False, v=val: self._set("notify", "phone", v))
        setup = nm.addAction("Phone setup…")
        setup.triggered.connect(self.phone_setup)
        test = nm.addAction("Send a test push")
        test.triggered.connect(self.test_push)

        s = m.addMenu("Settings")

        s.addSection("Card order")
        grp = QActionGroup(s)
        for label, val in (("Recent first (alt-tab)", "mru"), ("Stable (host, then age)", "stable")):
            a = s.addAction(label)
            a.setCheckable(True)
            a.setChecked(cfg.ui.get("order", "mru") == val)
            a.setActionGroup(grp)
            a.triggered.connect(lambda _=False, v=val: self._set("ui", "order", v))
        s.addSeparator()

        self.tabbar = s.addAction("Kitty tab bar")
        self.tabbar.setCheckable(True)
        self.tabbar.setChecked(bool(cfg.terminal.get("kitty_tab_bar", True)))
        self.tabbar.toggled.connect(lambda v: self._set("terminal", "kitty_tab_bar", v))

        self.autoname = s.addAction("Auto-name sessions from their title")
        self.autoname.setCheckable(True)
        self.autoname.setChecked(bool(cfg.ui.get("auto_name", True)))
        self.autoname.toggled.connect(lambda v: self._set("ui", "auto_name", v))
        self.hidden = s.addAction("Show hidden sessions")
        self.hidden.setCheckable(True)
        self.hidden.toggled.connect(lambda v: (setattr(self.ctl.store, "show_hidden", v),
                                               self.ctl.overlay.on_store_changed()))

        from .cli import AUTOSTART
        self.login = s.addAction("Start at login")
        self.login.setCheckable(True)
        from .cli import service_installed, systemctl
        self.login.setChecked(systemctl("is-enabled", "termwall").stdout.strip() == "enabled"
                              if service_installed() else os.path.exists(AUTOSTART))
        self.login.toggled.connect(self._set_login)
        s.addSeparator()
        edit = s.addAction("Edit config file…")
        edit.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.CONFIG_PATH))))
        reload_ = s.addAction("Reload config (restart)")
        reload_.triggered.connect(self.ctl.restart)

        m.addSeparator()
        q = m.addAction("Quit termwall")
        q.triggered.connect(self.ctl.quit)

    # ---------------------------------------------------------------- phone + stale
    def phone_setup(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        n = self.ctl.cfg.notify
        topic = n.get("ntfy_topic", "")
        if not topic:
            import secrets
            topic = "termwall-" + secrets.token_hex(8)
            self._set("notify", "ntfy_topic", topic)
        url = f"{n.get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{topic}"
        QApplication.clipboard().setText(url)
        QMessageBox.information(None, "termwall — phone alerts",
            "1. Install the ntfy app (Android / iOS).\n"
            f"2. Subscribe to:\n\n    {url}\n\n   (copied to the clipboard)\n\n"
            "termwall pushes only the host and session name, and only while you're away "
            "(screen locked or idle for a few minutes). Use “Send a test push” to check.")

    def test_push(self):
        from . import presence
        n = self.ctl.cfg.notify
        err = presence.push(n.get("ntfy_server", "https://ntfy.sh"), n.get("ntfy_topic", ""),
                            "termwall: test", "If you can read this, phone alerts work.", False)
        self.tray.showMessage("termwall", f"test push failed: {err}" if err else "test push sent")

    def close_stale(self):
        from PySide6.QtWidgets import QMessageBox
        stale = self.ctl.store.stale()
        if not stale:
            return
        days = self.ctl.cfg.ui.get("stale_days", 7)
        names = "\n".join(f"  {s.host}: {s.name} — {s.title or ''}"[:90] for s in stale)
        r = QMessageBox.question(None, "termwall — stale sessions",
                                 f"Close these tmux sessions? Idle > {days} days, not attached, "
                                 f"not pinned. Their conversations stay resumable from Recent.\n\n{names}")
        if r == QMessageBox.Yes:
            for s in stale:
                self.ctl.conns[s.host].send("kill", target=s.tmux_id)

    def show_help(self):
        self.ctl.overlay.summon()
        self.ctl.overlay.help = True
        self.ctl.overlay.update()

    def _set(self, section, key, value):
        config.set_option(section, key, value)
        getattr(self.ctl.cfg, section)[key] = value   # live, no restart needed
        self.ctl.launcher.cfg = self.ctl.cfg.terminal
        self.ctl.overlay.update()

    def _set_login(self, on):
        from .cli import AUTOSTART, self_path, service_installed, systemctl
        if service_installed():
            systemctl("enable" if on else "disable", "termwall")
            return
        if on:
            os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
            with open(AUTOSTART, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=termwall\n"
                        f"Exec={self_path()} daemon\nIcon=termwall\n"
                        "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
        elif os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)

    # ---------------------------------------------------------------- live state
    def refresh(self):
        store = self.ctl.store
        live = store.live()
        waiting = [s for s in live if store.needs_you(s)]
        busy = [s for s in live if s.status == "busy"]
        for h, a in self.host_actions.items():
            st = store.host_state.get(h, "")
            n = sum(1 for s in live if s.host == h)
            dot = "●" if st == "online" else "○"
            a.setText(f"{dot}  {h} — {n} live" if st == "online" else f"{dot}  {h} — {st}")
        if bool(waiting) != self.badge:
            self.badge = bool(waiting)
            self.tray.setIcon(self._icon(self.badge))
        usage = store.codex_usage()
        for a, line in zip(self.usage_actions, usage + [""] * 2):
            a.setText(line)
            a.setVisible(bool(line))
        stale = store.stale()
        self.stale_action.setText(f"Close {len(stale)} stale session{'s' if len(stale) != 1 else ''}…")
        self.stale_action.setVisible(bool(stale))
        tip = f"termwall · {len(live)} live · {len(busy)} working"
        if waiting:
            tip += f" · {len(waiting)} waiting: " + ", ".join(s.title or s.name for s in waiting[:3])
        if usage:
            tip += "\n" + "\n".join(usage)
        self.tray.setToolTip(tip)
