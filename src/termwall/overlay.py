"""The fullscreen overlay: a custom-painted grid of session cards."""
from __future__ import annotations

import math
import re
import threading
import time

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetricsF, QGuiApplication,
                           QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import (QApplication, QComboBox, QCompleter, QFrame, QGridLayout, QLabel,
                               QLineEdit, QWidget)

from . import render, theme
from .model import Session, Store

UI_FONT = "Ubuntu Sans"
GLYPH = {"claude": "✻", "codex": "◆", "shell": "❯"}
AGENT_COLOR = {"claude": theme.CLAUDE, "codex": theme.CODEX, "shell": theme.SHELL}
CARD_ASPECT = 1.55
GAP = 22


def ui_font(px, weight=QFont.Normal):
    f = QFont(UI_FONT)
    f.setPixelSize(int(px))
    f.setWeight(weight)
    return f


def ago(ts: float) -> str:
    if not ts:
        return ""
    d = max(0, time.time() - ts)
    if d < 45:
        return "now"
    if d < 3600:
        return f"{int(d // 60) or 1}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def lerp_rect(a: QRectF, b: QRectF, t: float) -> QRectF:
    return QRectF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t,
                  a.width() + (b.width() - a.width()) * t,
                  a.height() + (b.height() - a.height()) * t)


def fuzzy(q: str, s: Session) -> bool:
    hay = " ".join([s.title, s.name, s.short_cwd, s.host, s.agent, s.last_user, s.model, s.model_label]).lower()
    return all(term in hay for term in q.lower().split())


def slug(text: str, n=3) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "-".join(words[:n]) or "session"


class NewPanel(QFrame):
    """Ctrl+N: host / directory / agent / name."""
    submitted = Signal(dict)
    cancelled = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(f"""
            #panel {{ background: {theme.CARD_BG}; border: 1px solid {theme.ACCENT}; border-radius: 14px; }}
            QLabel {{ color: {theme.MUTED}; font: 13px "{UI_FONT}"; }}
            QLabel#h {{ color: {theme.TEXT}; font: 600 18px "{UI_FONT}"; }}
            QLineEdit, QComboBox {{ background: {theme.TERM_BG}; color: {theme.TEXT}; border: 1px solid {theme.CARD_BORDER};
                border-radius: 7px; padding: 7px 9px; font: 14px "{UI_FONT}"; selection-background-color: {theme.ACCENT}; }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {theme.ACCENT}; }}
            QComboBox QAbstractItemView {{ background: {theme.TERM_BG}; color: {theme.TEXT}; selection-background-color: {theme.ACCENT}; }}
        """)
        g = QGridLayout(self)
        g.setContentsMargins(22, 18, 22, 18)
        g.setVerticalSpacing(10)
        h = QLabel("New session", objectName="h")
        g.addWidget(h, 0, 0, 1, 2)
        self.host = QComboBox()
        self.agent = QComboBox()
        self.agent.addItems(["claude", "codex", "shell"])
        self.cwd = QLineEdit()
        self.name = QLineEdit(placeholderText="(from directory)")
        for r, (lbl, w) in enumerate([("Host", self.host), ("Agent", self.agent),
                                      ("Directory", self.cwd), ("Name", self.name)], 1):
            g.addWidget(QLabel(lbl), r, 0)
            g.addWidget(w, r, 1)
        hint = QLabel("↵ create & open   ·   esc cancel")
        g.addWidget(hint, 5, 0, 1, 2)
        g.setColumnMinimumWidth(1, 420)
        self.completer = QCompleter([])
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.cwd.setCompleter(self.completer)
        self.dirs_by_host: dict[str, list[str]] = {}
        self.host.currentTextChanged.connect(self._host_changed)
        self.hide()

    def _host_changed(self, host):
        from PySide6.QtCore import QStringListModel
        dirs = self.dirs_by_host.get(host, []) or ["~"]
        self.completer.setModel(QStringListModel(dirs))
        # a path from another machine is meaningless here: use this host's latest dir
        if self.isVisible() or not self.cwd.text():
            self.cwd.setText(dirs[0])
            self.cwd.selectAll()

    def open(self, hosts, host, cwd, agent, dirs_by_host):
        self.dirs_by_host = dirs_by_host
        self.host.clear()
        self.host.addItems(hosts)
        self.host.setCurrentText(host)
        self._host_changed(host)
        self.agent.setCurrentText(agent if agent in ("claude", "codex") else "claude")
        self.cwd.setText(cwd or "~")
        self.name.clear()
        self.adjustSize()
        p = self.parentWidget()
        self.move((p.width() - self.width()) // 2, int(p.height() * 0.28))
        self.show()
        self.raise_()
        self.cwd.setFocus()
        self.cwd.selectAll()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.completer.popup().isVisible():
                return super().keyPressEvent(e)
            self.submitted.emit({"host": self.host.currentText(), "agent": self.agent.currentText(),
                                 "cwd": self.cwd.text().strip(), "name": self.name.text().strip()})
        else:
            super().keyPressEvent(e)


class RenameEdit(QLineEdit):
    done = Signal(str)
    cancelled = Signal()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.done.emit(self.text().strip())
        else:
            super().keyPressEvent(e)


class Overlay(QWidget):
    open_requested = Signal(object)          # Session
    new_requested = Signal(dict)
    kill_requested = Signal(object)
    rename_requested = Signal(object, str)
    reply_requested = Signal(object, str)    # Session, text ("" = just Enter)
    search_requested = Signal(str)           # transcript search ("" = clear)
    peek_requested = Signal(object)          # Session: fetch scrollback / transcript
    visibility_changed = Signal(bool)

    def __init__(self, store: Store):
        flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        if QGuiApplication.platformName() == "xcb":
            # override-redirect like rofi: the WM would otherwise auto-maximize a
            # screen-sized window onto whichever monitor has focus
            flags |= Qt.X11BypassWindowManagerHint
        super().__init__(None, flags)
        self.setWindowTitle("termwall")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.store = store
        self.mode = "live"            # "live" | "recent"
        self.host_filter: str | None = None
        self.query = ""
        self.items: list[Session] = []
        self.rects: list[QRectF] = []
        self.sel = 0
        self.sel_key: str | None = None
        self.peek = False
        self.scroll = 0.0
        self.max_scroll = 0.0
        self.thumbs: dict[str, tuple[str, object]] = {}
        self.big: dict[str, tuple[str, object]] = {}
        self.toast_text = ""
        self.toast_until = 0.0
        self.kill_armed: str | None = None
        self.chip_rects: list[tuple[QRectF, str, object]] = []
        self.last_mouse = QPoint()
        self.open_t = 0.0
        self.opening: Session | None = None
        self.appear_t = 1.0
        self.help = False
        self.peek_scroll = 0.0         # px scrolled up from the bottom of the peek
        self.peek_content: dict[str, tuple] = {}   # key -> ("screen", text) | ("chat", msgs)
        self.peek_imgs: dict[str, tuple] = {}
        self.search_timer = QTimer(self, singleShot=True, interval=350,
                                   timeout=lambda: self.search_requested.emit(self.query))

        self.cursor_rect = QRectF()
        self.cursor_from = QRectF()
        self.cursor_anim = QVariantAnimation(self, duration=140, startValue=0.0, endValue=1.0,
                                             easingCurve=QEasingCurve.OutCubic)
        self.cursor_anim.valueChanged.connect(self._cursor_step)
        self.cursor_t = 1.0
        self.open_anim = QVariantAnimation(self, duration=210, startValue=0.0, endValue=1.0,
                                           easingCurve=QEasingCurve.InOutCubic)
        self.open_anim.valueChanged.connect(self._open_step)
        self.open_anim.finished.connect(self._open_done)
        self.appear_anim = QVariantAnimation(self, duration=170, startValue=0.0, endValue=1.0,
                                             easingCurve=QEasingCurve.OutCubic)
        self.appear_anim.valueChanged.connect(lambda v: (setattr(self, "appear_t", v), self.update()))
        self.peek_anim = QVariantAnimation(self, duration=160, startValue=0.0, endValue=1.0,
                                           easingCurve=QEasingCurve.OutCubic)
        self.peek_anim.valueChanged.connect(lambda v: (setattr(self, "peek_t", v), self.update()))
        self.peek_t = 0.0
        self.pulse = 0.0
        self.pulse_timer = QTimer(self, interval=50, timeout=self._pulse)
        self.clock = QTimer(self, interval=15000, timeout=self.update)  # refresh "3m ago"

        self.new_panel = NewPanel(self)
        self.new_panel.submitted.connect(self._new_submitted)
        self.new_panel.cancelled.connect(self._close_panels)
        self.rename_edit = RenameEdit(self)
        self.rename_edit.setStyleSheet(
            f"background:{theme.TERM_BG};color:{theme.TEXT};border:1px solid {theme.ACCENT};"
            f"border-radius:6px;padding:4px 8px;font:600 15px '{UI_FONT}';")
        self.rename_edit.hide()
        self.rename_edit.done.connect(self._rename_done)
        self.rename_edit.cancelled.connect(self._close_panels)
        self.reply_edit = RenameEdit(self)
        self.reply_edit.setPlaceholderText("reply\u2026  (\u21b5 sends, empty \u21b5 = press Enter, esc cancels)")
        self.reply_edit.setStyleSheet(
            f"background:{theme.TERM_BG};color:{theme.TEXT};border:2px solid {theme.WAITING};"
            f"border-radius:8px;padding:8px 10px;font:15px 'DejaVu Sans Mono';")
        self.reply_edit.hide()
        self.reply_edit.done.connect(self._reply_done)
        self.reply_edit.cancelled.connect(self._close_panels)

    # ================================================================ show/hide
    def toggle(self):
        if self.isVisible() and not self.opening:
            self.dismiss()
        else:
            self.summon()

    def summon(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if self.isVisible():
            self.hide()
        self.winId()  # make sure the native window exists so we can pin its screen
        self.windowHandle().setScreen(screen)
        self.setGeometry(screen.geometry())
        self.query = ""
        self.mode = "live"
        self.host_filter = None
        self.peek_scroll = 0
        self.help = False
        self.store.search_q = ""
        self.store.search = {}
        self.peek = False
        self.peek_t = 0.0
        self.opening = None
        self.open_t = 0.0
        self.scroll = 0.0
        self.sel_key = None
        self._close_panels()
        self.refresh(preselect=True)
        self.appear_t = 0.0
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.appear_anim.start()
        self.pulse_timer.start()
        self.clock.start()
        self.last_mouse = QCursor.pos()
        self.visibility_changed.emit(True)
        QTimer.singleShot(60, self._ensure_focus)

    def _ensure_focus(self):
        if self.isVisible() and not self.isActiveWindow():
            self.raise_()
            self.activateWindow()
            if self.windowHandle():
                self.windowHandle().requestActivate()

    def dismiss(self):
        self._close_panels()
        self.pulse_timer.stop()
        self.clock.stop()
        self.hide()
        self.visibility_changed.emit(False)

    def changeEvent(self, e):
        # click-away / alt-tab hides the overlay, like a launcher
        if e.type() == e.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            if not self.opening and not QApplication.activePopupWidget():
                QTimer.singleShot(120, lambda: (not self.isActiveWindow() and self.isVisible()
                                                and not self.opening and self.dismiss()))
        super().changeEvent(e)

    # ================================================================ data
    def refresh(self, preselect=False):
        if self.mode == "live":
            items = self.store.live(self.host_filter)
        else:
            items = self.store.recent(self.host_filter)
        if self.query:
            items = [s for s in items if s.snippet or fuzzy(self.query, s)]
        self.items = items
        if preselect:
            # alt-tab feel: preselect the session you were in before the current one
            self.sel = 0
            if (self.mode == "live" and self.store.cfg.ui.get("order") == "mru"
                    and items and self.store.state["opened"].get(items[0].key)):
                # the next attachable session after the one you're in
                nxt = [i for i, x in enumerate(items[1:], 1) if x.kind == "tmux"]
                self.sel = nxt[0] if nxt else 0
        elif self.sel_key:
            keys = [s.key for s in items]
            self.sel = keys.index(self.sel_key) if self.sel_key in keys else min(self.sel, max(0, len(items) - 1))
        self.sel = max(0, min(self.sel, len(items) - 1))
        self.sel_key = items[self.sel].key if items else None
        self.layout_cards()
        if self.sel < len(self.rects):
            if preselect or self.cursor_rect.isNull():
                self.cursor_rect = QRectF(self.rects[self.sel])
                self.cursor_from = QRectF(self.cursor_rect)
            elif self.cursor_anim.state() != QVariantAnimation.Running:
                self.cursor_rect = QRectF(self.rects[self.sel])
        self.update()

    def on_store_changed(self):
        if self.isVisible() and not self.opening:
            self.refresh()

    # ================================================================ layout
    def grid_area(self) -> QRectF:
        return QRectF(56, 128, self.width() - 112, self.height() - 128 - 64)

    def layout_cards(self):
        area = self.grid_area()
        n = len(self.items)
        self.rects = []
        if not n:
            self.max_scroll = 0
            return
        W, H = area.width(), area.height()
        best = None
        for cols in range(1, n + 1):
            w = min((W - (cols - 1) * GAP) / cols, 640)
            h = w / CARD_ASPECT
            rows = math.ceil(n / cols)
            if rows * h + (rows - 1) * GAP <= H and (best is None or w > best[1]):
                best = (cols, w, h)
        if best is None:
            cols = max(1, int((W + GAP) // (330 + GAP)))
            w = (W - (cols - 1) * GAP) / cols
            best = (cols, w, w / CARD_ASPECT)
        cols, w, h = best
        rows = math.ceil(n / cols)
        total_h = rows * h + (rows - 1) * GAP
        total_w = min(n, cols) * w + (min(n, cols) - 1) * GAP
        x0 = area.x() + (W - total_w) / 2
        y0 = area.y() + max(0, (H - total_h) / 2 - 20)
        self.max_scroll = max(0, total_h - H)
        self.cols = cols
        for i in range(n):
            r, c = divmod(i, cols)
            self.rects.append(QRectF(x0 + c * (w + GAP), y0 + r * (h + GAP) - self.scroll, w, h))

    def resizeEvent(self, e):
        self.layout_cards()
        super().resizeEvent(e)

    def _ensure_visible(self):
        if not self.rects or self.sel >= len(self.rects):
            return
        area = self.grid_area()
        r = self.rects[self.sel]
        if r.bottom() > area.bottom():
            self.scroll += r.bottom() - area.bottom() + 8
        elif r.top() < area.top():
            self.scroll -= area.top() - r.top() + 8
        self.scroll = max(0, min(self.scroll, self.max_scroll))
        self.layout_cards()

    def select(self, i, animate=True):
        if not self.items:
            return
        i = max(0, min(i, len(self.items) - 1))
        if i == self.sel and not self.cursor_rect.isNull():
            return
        self.sel = i
        self.sel_key = self.items[i].key
        self.kill_armed = None
        self._ensure_visible()
        if animate:
            self.cursor_from = QRectF(self.cursor_rect)
            self.cursor_anim.stop()
            self.cursor_anim.start()
        else:
            self.cursor_rect = QRectF(self.rects[i])
        self.update()

    def _cursor_step(self, t):
        if self.sel < len(self.rects):
            self.cursor_rect = lerp_rect(self.cursor_from, self.rects[self.sel], t)
        self.update()

    def _pulse(self):
        self.pulse = (self.pulse + 0.05) % 1.0
        if any(s.status == "busy" or self.store.needs_you(s) for s in self.items):
            self.update()

    # ================================================================ open
    def activate(self, i=None):
        if i is not None:
            self.select(i, animate=False)
        if not self.items or self.opening:
            return
        s = self.items[self.sel]
        if s.kind == "bare":
            where = f"{s.host} ({s.tty})" if s.tty and s.tty not in ("?", "??") else s.host
            self.toast(f"Running outside tmux on {where} — can't attach from here. "
                       f"Quit it there, then resume it from Recent (Tab).")
            return
        if s.offline:
            self.toast(f"{s.host} is offline")
            return
        self.opening = s
        self.open_requested.emit(s)
        if s.kind == "history":
            return  # the controller finishes with finish_open() once tmux is up
        self.cursor_from = QRectF(self.rects[self.sel])
        self.open_anim.start()

    def finish_open(self, ok: bool, msg: str = ""):
        """Controller callback for the async resume path."""
        if not ok:
            self.opening = None
            self.toast(msg or "failed")
            return
        if self.isVisible():
            self.cursor_from = QRectF(self.rects[self.sel]) if self.sel < len(self.rects) else QRectF(self.rect())
            self.open_anim.start()

    def _open_step(self, t):
        self.open_t = t
        self.update()

    def _open_done(self):
        self.dismiss()
        self.opening = None
        self.open_t = 0.0

    # ================================================================ panels
    def _close_panels(self):
        self.new_panel.hide()
        self.rename_edit.hide()
        self.reply_edit.hide()
        if self.isVisible():
            self.setFocus()

    def open_new_panel(self):
        s = self.items[self.sel] if self.items else None
        host = self.host_filter or (s.host if s else self.store.hosts[0])
        dirs = {h: self.store.recent_dirs(h) for h in self.store.hosts}
        cwd = s.short_cwd if s and s.host == host and s.cwd else dirs[host][0]
        self.new_panel.open(self.store.hosts, host, cwd, s.agent if s else "claude", dirs)

    def _new_submitted(self, spec):
        self.new_panel.hide()
        self.setFocus()
        self.opening = Session(key="pending", host=spec["host"], kind="history", agent=spec["agent"])
        self.new_requested.emit(spec)

    def open_rename(self):
        if not self.items:
            return
        s = self.items[self.sel]
        if s.kind != "tmux":
            self.toast("Only tmux sessions can be renamed")
            return
        r = self.rects[self.sel]
        self.rename_edit.setText(s.name)
        self.rename_edit.setGeometry(int(r.x() + 10), int(r.y() + 6), int(r.width() - 20), 32)
        self.rename_edit.show()
        self.rename_edit.setFocus()
        self.rename_edit.selectAll()

    def _rename_done(self, name):
        s = self.items[self.sel] if self.items else None
        self._close_panels()
        if s and name and name != s.name:
            self.rename_requested.emit(s, name)

    def open_reply(self):
        if not self.items:
            return
        s = self.items[self.sel]
        if s.kind != "tmux" or s.offline:
            self.toast("Quick reply works for tmux sessions")
            return
        r = self.peek_rect() if self.peek else self.rects[self.sel]
        self.reply_edit.clear()
        self.reply_edit.setGeometry(int(r.x() + 14), int(r.bottom() - 58), int(r.width() - 28), 44)
        self.reply_edit.show()
        self.reply_edit.raise_()
        self.reply_edit.setFocus()

    def _reply_done(self, text):
        s = self.items[self.sel] if self.items else None
        self._close_panels()
        if s:
            self.reply_requested.emit(s, text)
            self.toast(f"sent to {s.host}:{s.name}" + ("" if text else " (Enter)"), 2)

    def toggle_pin(self):
        if self.items:
            s = self.items[self.sel]
            on = self.store.toggle("pinned", s)
            self.toast(("Pinned " if on else "Unpinned ") + (s.title or s.name), 2)
            self.sel_key = s.key
            self.refresh()

    def toggle_hide(self):
        if self.items:
            s = self.items[self.sel]
            on = self.store.toggle("hidden", s)
            self.toast(("Hidden: " if on else "Unhidden: ") + (s.title or s.name)
                       + ("   (ctrl+shift+H shows hidden)" if on else ""), 3)
            self.refresh()

    def toast(self, text, secs=4.0):
        self.toast_text = text
        self.toast_until = time.time() + secs
        self.update()
        QTimer.singleShot(int(secs * 1000) + 50, self.update)

    # ================================================================ input
    def keyPressEvent(self, e):
        k, mods = e.key(), e.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        if self.opening and k != Qt.Key_Escape:
            return
        if self.help:
            self.help = False
            self.update()
            return
        if k == Qt.Key_F1 or (e.text() == "?" and not self.query):
            self.help = True
            self.update()
            return
        if k == Qt.Key_Escape:
            if self.opening:
                self.opening = None
                self.open_anim.stop()
                self.open_t = 0
            elif self.peek:
                self.set_peek(False)
            elif self.query:
                self.query = ""
                self.query_changed()
            else:
                self.dismiss()
        elif k in (Qt.Key_Return, Qt.Key_Enter) and ctrl:
            self.open_reply()
        elif k in (Qt.Key_Return, Qt.Key_Enter):
            self.activate()
        elif k == Qt.Key_Space and not ctrl:
            self.set_peek(not self.peek)
        elif self.peek and k in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown,
                                 Qt.Key_Home, Qt.Key_End) and not ctrl:
            step = {Qt.Key_Up: 60, Qt.Key_Down: -60, Qt.Key_PageUp: 600, Qt.Key_PageDown: -600,
                    Qt.Key_Home: 1e9, Qt.Key_End: -1e9}[k]
            self.scroll_peek(step)
        elif self.peek and k in (Qt.Key_Left, Qt.Key_Right) and not ctrl:
            self.select(self.sel + (1 if k == Qt.Key_Right else -1))
            self.peek_scroll = 0
            self.peek_requested.emit(self.items[self.sel])
        elif k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and ctrl:
            self.cycle_host(1 if k in (Qt.Key_Right, Qt.Key_Down) else -1)
        elif k == Qt.Key_Left:
            self.select(self.sel - 1)
        elif k == Qt.Key_Right:
            self.select(self.sel + 1)
        elif k == Qt.Key_Up:
            self.select(self.sel - getattr(self, "cols", 1))
        elif k == Qt.Key_Down:
            self.select(self.sel + getattr(self, "cols", 1))
        elif k == Qt.Key_Home:
            self.select(0)
        elif k == Qt.Key_End:
            self.select(len(self.items) - 1)
        elif k == Qt.Key_Tab or k == Qt.Key_Backtab:
            self.set_mode("recent" if self.mode == "live" else "live")
        elif ctrl and k == Qt.Key_N:
            self.open_new_panel()
        elif ctrl and k == Qt.Key_R:
            self.open_rename()
        elif ctrl and k == Qt.Key_P:
            self.toggle_pin()
        elif ctrl and k == Qt.Key_H and mods & Qt.ShiftModifier:
            self.store.show_hidden = not self.store.show_hidden
            self.toast("Showing hidden sessions" if self.store.show_hidden else "Hidden sessions hidden", 2)
            self.refresh()
        elif ctrl and k == Qt.Key_H:
            self.toggle_hide()
        elif (ctrl and k == Qt.Key_K) or k == Qt.Key_Delete:
            self.try_kill()
        elif k == Qt.Key_F5:
            self.refresh()
        elif k == Qt.Key_Backspace:
            if self.query:
                self.query = self.query[:-1]
                self.query_changed()
        elif not ctrl and e.text() and e.text().isprintable():
            ch = e.text()
            if not self.query and ch.isdigit() and ch != "0":
                if int(ch) <= len(self.items):
                    self.activate(int(ch) - 1)
                return
            self.query += ch
            self.query_changed()
        else:
            super().keyPressEvent(e)

    def query_changed(self):
        self.sel = 0
        self.sel_key = None
        self.scroll = 0
        # full-text transcript search on every host (debounced); live sessions whose
        # transcript matches stay on the Live tab, ended ones show up in Recent
        self.store.search_q = self.query if len(self.query) >= 3 else ""
        self.store.search = {}
        self.search_timer.start()
        self.refresh()
        self.cursor_rect = QRectF(self.rects[0]) if self.rects else QRectF()

    def set_mode(self, mode):
        self.mode = mode
        self.sel = 0
        self.sel_key = None
        self.scroll = 0
        self.peek = False
        self.peek_t = 0
        self.refresh()
        if self.rects:
            self.cursor_rect = QRectF(self.rects[0])

    def cycle_host(self, d):
        opts = [None] + self.store.hosts
        i = opts.index(self.host_filter) if self.host_filter in opts else 0
        self.host_filter = opts[(i + d) % len(opts)]
        self.sel = 0
        self.sel_key = None
        self.scroll = 0
        self.refresh()
        if self.rects:
            self.cursor_rect = QRectF(self.rects[0])

    def set_peek(self, on):
        if on and not self.items:
            return
        self.peek = on
        self.peek_scroll = 0
        if on:
            self.peek_t = 0.0
            self.peek_anim.start()
            self.peek_requested.emit(self.items[self.sel])
        self.update()

    def set_peek_content(self, key, kind, data):
        self.peek_content[key] = (kind, data)
        self.peek_imgs.pop(key, None)
        self.update()

    def scroll_peek(self, dy):
        was = self.peek_scroll
        self.peek_scroll = max(0.0, self.peek_scroll + dy)
        if was == 0 and self.peek_scroll > 0 and self.items and self.items[self.sel].kind == "tmux":
            # leaving the live bottom: refresh the scrollback snapshot
            self.peek_requested.emit(self.items[self.sel])
        self.update()

    def try_kill(self):
        if not self.items:
            return
        s = self.items[self.sel]
        if s.kind != "tmux":
            self.toast("Only tmux sessions can be killed from here")
            return
        if self.kill_armed == s.key:
            self.kill_armed = None
            self.kill_requested.emit(s)
            self.toast(f"Killed {s.host}:{s.name}")
        else:
            self.kill_armed = s.key
            self.toast(f"Press Ctrl+K again to kill {s.host}:{s.name}", 3)

    def mouseMoveEvent(self, e):
        if e.globalPosition().toPoint() == self.last_mouse:
            return
        self.last_mouse = e.globalPosition().toPoint()
        if self.peek or self.opening:
            return
        i = self.card_at(e.position())
        if i is not None and i != self.sel:
            self.select(i)

    def card_at(self, pos):
        for i, r in enumerate(self.rects):
            if r.contains(pos):
                return i
        return None

    def mousePressEvent(self, e):
        if self.new_panel.isVisible() or self.rename_edit.isVisible():
            self._close_panels()
            return
        pos = e.position()
        for r, kind, val in self.chip_rects:
            if r.contains(pos):
                if kind == "mode":
                    self.set_mode(val)
                else:
                    self.host_filter = val
                    self.set_mode(self.mode)
                return
        if self.peek:
            if e.button() == Qt.RightButton or not self.peek_rect().contains(pos):
                self.set_peek(False)
            else:
                self.activate()
            return
        i = self.card_at(pos)
        if i is None:
            self.dismiss()
        elif e.button() == Qt.RightButton:
            self.select(i, animate=False)
            self.set_peek(True)
        else:
            self.activate(i)

    def wheelEvent(self, e):
        if self.peek:
            self.scroll_peek(e.angleDelta().y())
            return
        if self.max_scroll:
            self.scroll = max(0, min(self.max_scroll, self.scroll - e.angleDelta().y() / 2))
            self.layout_cards()
            if self.sel < len(self.rects):
                self.cursor_rect = QRectF(self.rects[self.sel])
            self.update()

    # ================================================================ painting
    def peek_rect(self) -> QRectF:
        W, H = self.width(), self.height()
        w = min(W * 0.86, H * 0.86 * 1.9)
        h = min(H * 0.84, w / 1.6)
        return QRectF((W - w) / 2, (H - h) / 2 + 24, w, h)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        a = self.appear_t
        fade = 1.0 - self.open_t
        bg = QColor(*theme.OVERLAY_BG)
        bg.setAlphaF(bg.alphaF() * a * (0.35 + 0.65 * fade))
        p.fillRect(self.rect(), bg)
        # faint grid lines: the "minority report" glass
        p.setOpacity(0.05 * a * fade)
        p.setPen(QPen(QColor(theme.ACCENT), 1))
        for x in range(0, self.width(), 48):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 48):
            p.drawLine(0, y, self.width(), y)
        p.setOpacity(a * fade)
        self.paint_header(p)
        if not self.items:
            self.paint_empty(p)
        # cursor glow
        if self.items and self.sel < len(self.rects):
            self.paint_cursor(p, self.cursor_rect)
        for i, (s, r) in enumerate(zip(self.items, self.rects)):
            if r.bottom() < 100 or r.top() > self.height():
                continue
            if self.opening and i == self.sel:
                continue
            scale = 0.94 + 0.06 * a
            rr = QRectF(r.center().x() - r.width() * scale / 2, r.center().y() - r.height() * scale / 2,
                        r.width() * scale, r.height() * scale)
            self.paint_card(p, s, rr, i)
        p.setOpacity(1.0)
        if self.peek and self.items:
            self.paint_peek(p)
        if self.opening and self.sel < len(self.items) and self.open_t > 0:
            target = QRectF(self.rect())
            self.paint_card(p, self.items[self.sel], lerp_rect(self.cursor_from, target, self.open_t),
                            self.sel, zoom=True)
        if self.help:
            self.paint_help(p)
        self.paint_toast(p)
        p.end()

    def paint_header(self, p: QPainter):
        self.chip_rects = []
        p.setFont(ui_font(26, QFont.DemiBold))
        p.setPen(QColor(theme.TEXT))
        p.drawText(QPointF(56, 66), "termwall")
        fm = QFontMetricsF(p.font())
        x = 56 + fm.horizontalAdvance("termwall") + 34
        # mode chips
        for label, val in (("Live", "live"), ("Recent", "recent")):
            x = self.chip(p, x, 44, label, self.mode == val, theme.ACCENT, ("mode", val)) + 8
        x += 22
        for label, val in [("All", None)] + [(h, h) for h in self.store.hosts]:
            color = self.host_color(val) if val else theme.ACCENT
            st = self.store.host_state.get(val, "online") if val else "online"
            dot = None if not val else (theme.BUSY if st == "online" else
                                        theme.WAITING if st == "connecting" else theme.OFFLINE)
            x = self.chip(p, x, 44, label, self.host_filter == val, color, ("host", val), dot) + 8
        # search
        p.setFont(ui_font(22))
        if self.query:
            p.setPen(QColor(theme.TEXT))
            txt = f"⌕  {self.query}▁"
        else:
            p.setPen(QColor(theme.MUTED))
            txt = "type to filter"
        p.drawText(QRectF(0, 40, self.width() - 56, 40), Qt.AlignRight | Qt.AlignVCenter, txt)
        # hints
        p.setFont(ui_font(13))
        p.setPen(QColor(theme.MUTED))
        if self.peek:
            hints = ("\u2191\u2193 pgup pgdn scroll    \u2190\u2192 next session    \u21b5 open    "
                     "ctrl+\u21b5 reply    space/esc close peek")
        else:
            hints = ("\u21b5 open    ctrl+\u21b5 reply    space peek    tab live/recent    ctrl+\u2190\u2192 host    "
                     "ctrl+N new    ctrl+R rename    ctrl+P pin    ctrl+H hide    ctrl+K kill    ? all shortcuts")
        p.drawText(QRectF(0, self.height() - 44, self.width(), 24), Qt.AlignCenter, hints)

    def chip(self, p, x, y, label, active, color, hit, dot=None):
        p.setFont(ui_font(14, QFont.DemiBold if active else QFont.Normal))
        fm = QFontMetricsF(p.font())
        w = fm.horizontalAdvance(label) + 26 + (14 if dot else 0)
        r = QRectF(x, y, w, 30)
        c = QColor(color)
        if active:
            c.setAlphaF(0.22)
            p.setBrush(c)
            p.setPen(QPen(QColor(color), 1.2))
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(theme.CARD_BORDER), 1))
        p.drawRoundedRect(r, 15, 15)
        tx = x + 13
        if dot:
            p.setBrush(QColor(dot))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(tx + 3, y + 15), 3.5, 3.5)
            tx += 14
        p.setPen(QColor(theme.TEXT if active else theme.MUTED))
        p.drawText(QRectF(tx, y, w, 30), Qt.AlignVCenter, label)
        self.chip_rects.append((r, *hit))
        return x + w

    def host_color(self, host):
        try:
            return theme.HOST_COLORS[self.store.hosts.index(host) % len(theme.HOST_COLORS)]
        except ValueError:
            return theme.MUTED

    def paint_empty(self, p):
        p.setFont(ui_font(18))
        p.setPen(QColor(theme.MUTED))
        if self.query:
            msg = f"Nothing matches “{self.query}”"
        elif self.mode == "live":
            msg = "No live sessions.  Ctrl+N starts one, Tab shows recent sessions to resume."
        else:
            msg = "No recent sessions."
        p.drawText(self.grid_area(), Qt.AlignCenter, msg)

    def paint_cursor(self, p, r: QRectF):
        c = QColor(theme.ACCENT)
        for i, (grow, alpha) in enumerate(((14, 0.05), (9, 0.09), (5, 0.16), (2, 0.35))):
            c.setAlphaF(alpha)
            p.setPen(QPen(c, 2 if i == 3 else 6))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-grow, -grow, grow, grow), 14 + grow, 14 + grow)

    def status_of(self, s: Session):
        if s.offline:
            return theme.OFFLINE, "offline"
        if s.kind == "history":
            return theme.IDLE, "ended · ↵ resumes in tmux"
        if s.status == "approval":
            return theme.OFFLINE, "needs approval \u00b7 ctrl+\u21b5 replies"
        if s.agent == "shell":
            return theme.IDLE, s.cmd or "shell"
        if s.status == "busy":
            return theme.BUSY, "working"
        if self.store.is_stale(s):
            return theme.IDLE, f"stale \u00b7 idle {int((time.time() - s.updated) // 86400)}d"
        if self.store.needs_you(s):
            return theme.WAITING, "waiting for you"
        return theme.IDLE, "idle"

    def paint_card(self, p: QPainter, s: Session, r: QRectF, i: int, zoom=False):
        selected = i == self.sel
        radius = 12
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.CARD_BG))
        p.drawPath(path)
        scolor, stext = self.status_of(s)
        needs = scolor == theme.WAITING
        head_h = max(28.0, min(38.0, r.height() * 0.13))
        foot_h = head_h * 0.85
        # header
        p.setFont(ui_font(head_h * 0.42, QFont.DemiBold))
        p.setPen(QColor(AGENT_COLOR.get(s.agent, theme.TEXT)))
        p.drawText(QRectF(r.x() + 12, r.y(), 22, head_h), Qt.AlignVCenter, GLYPH.get(s.agent, "?"))
        hc = self.host_color(s.host)
        hfm = QFontMetricsF(ui_font(head_h * 0.34))
        hw = hfm.horizontalAdvance(s.host) + 16
        chip = QRectF(r.right() - hw - 10, r.y() + head_h * 0.2, hw, head_h * 0.6)
        cc = QColor(hc)
        cc.setAlphaF(0.18)
        p.setBrush(cc)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
        p.setFont(ui_font(head_h * 0.34))
        p.setPen(QColor(hc))
        p.drawText(chip, Qt.AlignCenter, s.host)
        # model chip, left of the host chip
        ml = s.model_label
        if ml:
            mfm = QFontMetricsF(ui_font(head_h * 0.32))
            mw = mfm.horizontalAdvance(ml) + 14
            if mw < r.width() * 0.4:
                mchip = QRectF(chip.left() - mw - 6, chip.top(), mw, chip.height())
                mc = QColor(AGENT_COLOR.get(s.agent, theme.MUTED))
                p.setBrush(Qt.NoBrush)
                mc.setAlphaF(0.55)
                p.setPen(QPen(mc, 1))
                p.drawRoundedRect(mchip, mchip.height() / 2, mchip.height() / 2)
                p.setFont(ui_font(head_h * 0.32))
                mc.setAlphaF(0.95)
                p.setPen(mc)
                p.drawText(mchip, Qt.AlignCenter, ml)
                chip = mchip
        title = s.title or s.name or "(untitled)"
        if s.ident in self.store.state["pinned"]:
            title = "\u2605 " + title
        p.setFont(ui_font(head_h * 0.42, QFont.DemiBold))
        p.setPen(QColor(theme.TEXT))
        fm = QFontMetricsF(p.font())
        tw = chip.left() - (r.x() + 36) - 8
        p.drawText(QRectF(r.x() + 36, r.y(), tw, head_h), Qt.AlignVCenter,
                   fm.elidedText(title, Qt.ElideRight, tw))
        # body (terminal thumbnail or transcript preview)
        body = QRectF(r.x() + 8, r.y() + head_h, r.width() - 16, r.height() - head_h - foot_h)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(body, 6, 6)
        p.setClipPath(clip)
        p.fillRect(body, QColor(theme.TERM_BG))
        if zoom and self.peek and not self.opening and self.peek_t >= 1 and self.paint_peek_body(p, s, body):
            pass
        elif s.kind == "tmux" and s.screen:
            self.paint_screen(p, s, body, big=zoom)
        else:
            self.paint_transcript(p, s, body, big=zoom)
        if s.offline or (s.ident in self.store.state["hidden"]):
            p.fillRect(body, QColor(0, 0, 0, 120))
        p.restore()
        # context meter: thin bar along the bottom of the body
        frac = self.store.ctx_frac(s)
        if frac is not None:
            bar = QRectF(body.x() + 2, body.bottom() - 3, (body.width() - 4), 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 18))
            p.drawRoundedRect(bar, 1.5, 1.5)
            col = theme.BUSY if frac < 0.6 else theme.WAITING if frac < 0.85 else theme.OFFLINE
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * frac, bar.height()), 1.5, 1.5)
        # footer
        fy = r.bottom() - foot_h
        p.setBrush(QColor(scolor))
        p.setPen(Qt.NoPen)
        rad = 4.0
        if s.status == "busy" and not s.offline:
            rad = 3.5 + 1.5 * math.sin(self.pulse * 2 * math.pi)
        p.drawEllipse(QPointF(r.x() + 18, fy + foot_h / 2), rad, rad)
        p.setFont(ui_font(foot_h * 0.42))
        p.setPen(QColor(scolor if scolor != theme.IDLE else theme.MUTED))
        sfm = QFontMetricsF(p.font())
        label = stext
        if s.kind == "bare":
            label = f"{stext} · outside tmux"
        if s.kind == "tmux" and s.attached:
            label += " · attached"
        p.drawText(QRectF(r.x() + 30, fy, r.width(), foot_h), Qt.AlignVCenter, label)
        lw = sfm.horizontalAdvance(label)
        p.setPen(QColor(theme.MUTED))
        right = ago(s.updated)
        if frac is not None:
            right = f"ctx {int(frac * 100)}% \u00b7 {right}"
        rw = sfm.horizontalAdvance(right)
        p.drawText(QRectF(r.right() - rw - 14, fy, rw + 4, foot_h), Qt.AlignVCenter, right)
        cwd_w = r.width() - lw - rw - 70
        if cwd_w > 40:
            cwd = s.short_cwd
            if s.git:
                cwd = f"{s.git['repo']} \u2387 {s.git['branch']}"
                n = self.store.repo_count(s)
                if n > 1:
                    cwd += f" ({n} in repo)"
            if s.kind == "tmux" and s.name:
                cwd = f"{s.name} · {cwd}"
            p.drawText(QRectF(r.x() + 30 + lw + 14, fy, cwd_w, foot_h), Qt.AlignVCenter,
                       sfm.elidedText(cwd, Qt.ElideMiddle, cwd_w))
        # number badge
        if i < 9 and not zoom and not self.query:
            p.setFont(ui_font(11, QFont.Bold))
            br = QRectF(r.x() - 7, r.y() - 7, 18, 18)
            p.setBrush(QColor(theme.ACCENT if selected else theme.CARD_BORDER))
            p.setPen(Qt.NoPen)
            p.drawEllipse(br)
            p.setPen(QColor(theme.TERM_BG if selected else theme.TEXT))
            p.drawText(br, Qt.AlignCenter, str(i + 1))
        # border
        p.setBrush(Qt.NoBrush)
        if s.status == "approval" and not s.offline:
            c = QColor(theme.OFFLINE)
            c.setAlphaF(0.55 + 0.4 * math.sin(self.pulse * 2 * math.pi))
            p.setPen(QPen(c, 2.5))
        elif needs:
            c = QColor(theme.WAITING)
            c.setAlphaF(0.45 + 0.4 * math.sin(self.pulse * 2 * math.pi))
            p.setPen(QPen(c, 2))
        elif self.kill_armed == s.key:
            p.setPen(QPen(QColor(theme.OFFLINE), 2))
        else:
            p.setPen(QPen(QColor(theme.ACCENT if selected else theme.CARD_BORDER), 1.5 if selected else 1))
        if s.kind in ("bare", "history"):
            pen = p.pen()
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
        p.drawPath(path)

    def screen_image(self, s: Session, big: bool):
        cache = self.big if big else self.thumbs
        hit = cache.get(s.key)
        if hit and hit[0] == s.screen_hash:
            return hit[1]
        img = render.render(s.screen, s.cols, s.rows, px=14 if big else 10, bottom_rows=s.rows)
        cache[s.key] = (s.screen_hash, img)
        return img

    def paint_screen(self, p: QPainter, s: Session, body: QRectF, big=False):
        img = self.screen_image(s, big)
        if big:
            scale = min(body.width() / img.width(), body.height() / img.height(), 1.6)
        else:
            # a viewport, not a shrunken screenshot: keep text near-legible and
            # show the bottom-left, where the agent is talking
            scale = max(0.62, min(1.0, body.width() / img.width()))
        src_w = min(img.width(), body.width() / scale)
        src_h = min(img.height(), body.height() / scale)
        target = QRectF(body.x(), body.y(), src_w * scale, src_h * scale)
        if big and src_h * scale < body.height():
            target.moveTop(body.y() + (body.height() - src_h * scale) / 2)
        p.drawImage(target, img, QRectF(0, img.height() - src_h, src_w, src_h))

    def paint_transcript(self, p: QPainter, s: Session, body: QRectF, big=False):
        font, fm = render.mono(17 if big else max(9, min(12, body.height() / 13)))
        p.setFont(font)
        pad = 10
        r = body.adjusted(pad, pad, -pad, -pad)
        y = r.y()
        if s.snippet:
            p.setPen(QColor(theme.WAITING))
            txt = "\u2315 " + s.snippet
            br = fm.boundingRect(r, Qt.TextWordWrap, txt)
            h = min(br.height(), fm.height() * (8 if big else 4))
            p.drawText(QRectF(r.x(), y, r.width(), h), Qt.TextWordWrap, txt)
            y += h + fm.height() * 0.6
        if s.last_user:
            p.setPen(QColor(theme.ACCENT))
            txt = "❯ " + s.last_user
            br = fm.boundingRect(r, Qt.TextWordWrap, txt)
            h = min(br.height(), fm.height() * (8 if big else 3))
            p.drawText(QRectF(r.x(), y, r.width(), h), Qt.TextWordWrap, txt)
            y += h + fm.height() * 0.6
        if s.last_assistant:
            p.setPen(QColor(theme.TERM_FG))
            p.drawText(QRectF(r.x(), y, r.width(), r.bottom() - y), Qt.TextWordWrap,
                       "⏺ " + s.last_assistant)
        # fade the overflow
        g = QLinearGradient(0, body.bottom() - 30, 0, body.bottom())
        c = QColor(theme.TERM_BG)
        c.setAlpha(0)
        g.setColorAt(0, c)
        g.setColorAt(1, QColor(theme.TERM_BG))
        p.fillRect(QRectF(body.x(), body.bottom() - 30, body.width(), 30), QBrush(g))

    def peek_image(self, s: Session, width: float):
        """Image of the peek content (scrollback or chat), cached."""
        content = self.peek_content.get(s.key)
        if not content or (s.kind == "tmux" and self.peek_scroll == 0):
            return None  # tmux at the live bottom: the normal big screen render
        kind, data = content
        sig = (kind, id(data), int(width))
        hit = self.peek_imgs.get(s.key)
        if hit and hit[0] == sig:
            return hit[1]
        if kind == "screen":
            img = render.render(data, s.cols, 0, px=14, bottom_rows=100000)
        else:
            img = self.render_chat(data, int(width))
        self.peek_imgs[s.key] = (sig, img)
        return img

    def render_chat(self, msgs, width):
        from PySide6.QtGui import QImage
        font, fm = render.mono(15)
        pad, gap = 18, 14
        inner = width - 2 * pad
        blocks = []
        h = pad
        for m in msgs:
            prefix = "\u276f " if m["role"] == "user" else "\u23fa "
            rect = fm.boundingRect(QRectF(0, 0, inner, 1e6), Qt.TextWordWrap, prefix + m["text"])
            blocks.append((m["role"], prefix + m["text"], h, rect.height()))
            h += rect.height() + gap
        img = QImage(width, int(max(h + pad, 10)), QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(theme.TERM_BG))
        q = QPainter(img)
        q.setRenderHint(QPainter.TextAntialiasing)
        q.setFont(font)
        for role, text, y, bh in blocks:
            q.setPen(QColor(theme.ACCENT if role == "user" else theme.TERM_FG))
            q.drawText(QRectF(pad, y, inner, bh + 2), Qt.TextWordWrap, text)
        q.end()
        return img

    def paint_peek_body(self, p: QPainter, s: Session, body: QRectF) -> bool:
        img = self.peek_image(s, body.width())
        if img is None:
            return False
        scale = min(1.0, body.width() / img.width())
        view_h = body.height() / scale
        max_off = max(0.0, img.height() - view_h)
        self.peek_scroll = min(self.peek_scroll, max_off)
        src_y = max(0.0, img.height() - view_h - self.peek_scroll)
        src_h = min(view_h, img.height())
        target = QRectF(body.x(), body.y(), img.width() * scale, src_h * scale)
        p.drawImage(target, img, QRectF(0, src_y, img.width(), src_h))
        if max_off > 0:  # scrollbar
            track = QRectF(body.right() - 6, body.y() + 6, 3, body.height() - 12)
            thumb_h = max(24.0, track.height() * view_h / img.height())
            pos = src_y / max_off
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 25))
            p.drawRoundedRect(track, 1.5, 1.5)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(track.x(), track.y() + (track.height() - thumb_h) * pos, 3, thumb_h), 1.5, 1.5)
        return True

    def paint_peek(self, p: QPainter):
        s = self.items[self.sel]
        t = self.peek_t
        dim = QColor(0, 0, 0, int(150 * t))
        p.fillRect(self.rect(), dim)
        src = self.rects[self.sel] if self.sel < len(self.rects) else self.peek_rect()
        self.paint_card(p, s, lerp_rect(src, self.peek_rect(), t), self.sel, zoom=True)

    HELP = [
        ("Anywhere", [
            ("Ctrl+Alt+Space", "show / hide the wall"),
            ("notification \u2192 Open", "jump to the session that needs you"),
        ]),
        ("On the wall", [
            ("\u21b5", "open (hotkey + \u21b5 = previous session)"),
            ("1 \u2013 9", "open card N"),
            ("type", "filter by title, folder, host, model"),
            ("\u2190 \u2191 \u2192 \u2193 / mouse", "move"),
            ("Tab", "Live \u21c4 Recent (\u21b5 resumes it)"),
            ("type in Recent", "3+ letters also search transcripts"),
            ("Ctrl+\u2190 / \u2192", "filter by host"),
            ("Space / right-click", "peek: \u2191\u2193 PgUp PgDn scroll, \u2190\u2192 next"),
            ("Ctrl+\u21b5", "quick reply (empty = press Enter, e.g. approve)"),
            ("Ctrl+N", "new session"),
            ("Ctrl+R", "rename"),
            ("Ctrl+P", "pin to front"),
            ("Ctrl+H / Ctrl+Shift+H", "hide / show hidden"),
            ("Ctrl+K \u00d72", "kill tmux session"),
            ("? or F1", "this sheet"),
            ("Esc", "close"),
        ]),
        ("In the termwall kitty window", [
            ("Ctrl+1 \u2013 9", "go to tab N"),
            ("Ctrl+Shift+\u2190 / \u2192", "previous / next tab"),
            ("Ctrl+b d", "detach (session keeps running)"),
        ]),
        ("In any terminal", [
            ("claude / codex", "starts inside tmux automatically"),
            ("TERMWALL_TMUX=0 claude", "run without tmux, once"),
        ]),
    ]

    def paint_help(self, p: QPainter):
        p.fillRect(self.rect(), QColor(0, 0, 0, 160))
        lh = 27
        cols = [[self.HELP[1]], [self.HELP[0], self.HELP[2], self.HELP[3]]]
        heights = [sum((len(items) + 1.6) * lh for _, items in col) for col in cols]
        colw = 560
        w = min(self.width() - 60, colw * 2 + 60)
        h = max(heights) + 70
        r = QRectF((self.width() - w) / 2, max(20, (self.height() - h) / 2), w, h)
        p.setBrush(QColor(theme.CARD_BG))
        p.setPen(QPen(QColor(theme.ACCENT), 1.5))
        p.drawRoundedRect(r, 16, 16)
        colw = (w - 60) / 2
        for ci, col in enumerate(cols):
            x0 = r.x() + 30 + ci * colw
            y = r.y() + 22
            for section, items in col:
                p.setFont(ui_font(13, QFont.DemiBold))
                p.setPen(QColor(theme.MUTED))
                p.drawText(QRectF(x0, y, colw, lh), Qt.AlignVCenter, section.upper())
                y += lh
                for key, what in items:
                    p.setFont(QFont("DejaVu Sans Mono", 10))
                    kw = QFontMetricsF(p.font()).horizontalAdvance(key) + 14
                    kr = QRectF(x0 + 4, y + 3, kw, lh - 6)
                    p.setBrush(QColor(theme.TERM_BG))
                    p.setPen(QPen(QColor(theme.CARD_BORDER), 1))
                    p.drawRoundedRect(kr, 5, 5)
                    p.setPen(QColor(theme.ACCENT))
                    p.drawText(kr, Qt.AlignCenter, key)
                    p.setFont(ui_font(13))
                    p.setPen(QColor(theme.TEXT))
                    tx = x0 + 4 + max(kw, 190) + 10
                    tw = x0 + colw - tx - 10
                    p.drawText(QRectF(tx, y, tw, lh), Qt.AlignVCenter,
                               QFontMetricsF(p.font()).elidedText(what, Qt.ElideRight, tw))
                    y += lh
                y += lh * 0.6
        p.setFont(ui_font(12))
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(r.x(), r.bottom() - 30, w, 24), Qt.AlignCenter,
                   "any key closes \u00b7 also in the tray menu \u00b7 README has the rest")

    def paint_toast(self, p: QPainter):
        if not self.toast_text or time.time() > self.toast_until:
            return
        p.setFont(ui_font(15))
        fm = QFontMetricsF(p.font())
        w = min(self.width() - 80, fm.horizontalAdvance(self.toast_text) + 40)
        r = QRectF((self.width() - w) / 2, self.height() - 110, w, 40)
        p.setBrush(QColor(theme.CARD_BG))
        p.setPen(QPen(QColor(theme.WAITING), 1))
        p.drawRoundedRect(r, 10, 10)
        p.setPen(QColor(theme.TEXT))
        p.drawText(r, Qt.AlignCenter, fm.elidedText(self.toast_text, Qt.ElideRight, w - 30))
