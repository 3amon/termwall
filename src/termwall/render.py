"""Render a tmux `capture-pane -e` screen (text + SGR escapes) to a QImage."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter

from . import theme

SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESC_RE = re.compile(r"\x1b(\[[0-9;:?]*[A-Za-ln-z@`]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][0-9A-Za-z])")


def _xterm256(i: int) -> QColor:
    if i < 16:
        return QColor(theme.ANSI[i])
    if i < 232:
        i -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return QColor(steps[i // 36], steps[(i // 6) % 6], steps[i % 6])
    v = 8 + (i - 232) * 10
    return QColor(v, v, v)


_PALETTE = [_xterm256(i) for i in range(256)]


class Style:
    __slots__ = ("fg", "bg", "bold", "dim", "rev", "ital")

    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = self.bg = None
        self.bold = self.dim = self.rev = self.ital = False

    def key(self):
        return (self.fg.rgba() if self.fg else None, self.bg.rgba() if self.bg else None,
                self.bold, self.dim, self.rev, self.ital)

    def apply(self, params: str):
        ps = [p for p in re.split(r"[;:]", params)] if params else ["0"]
        i = 0
        while i < len(ps):
            try:
                n = int(ps[i] or 0)
            except ValueError:
                n = 0
            if n == 0:
                self.reset()
            elif n == 1:
                self.bold = True
            elif n == 2:
                self.dim = True
            elif n == 3:
                self.ital = True
            elif n == 7:
                self.rev = True
            elif n == 22:
                self.bold = self.dim = False
            elif n == 23:
                self.ital = False
            elif n == 27:
                self.rev = False
            elif 30 <= n <= 37:
                self.fg = _PALETTE[n - 30]
            elif 90 <= n <= 97:
                self.fg = _PALETTE[n - 90 + 8]
            elif 40 <= n <= 47:
                self.bg = _PALETTE[n - 40]
            elif 100 <= n <= 107:
                self.bg = _PALETTE[n - 100 + 8]
            elif n == 39:
                self.fg = None
            elif n == 49:
                self.bg = None
            elif n in (38, 48) and i + 1 < len(ps):
                mode = ps[i + 1]
                col = None
                if mode == "5" and i + 2 < len(ps):
                    col = _PALETTE[int(ps[i + 2] or 0) % 256]
                    i += 2
                elif mode == "2" and i + 4 < len(ps):
                    # 38;2;r;g;b  (colon form may carry an empty colorspace slot)
                    vals = [int(x or 0) for x in ps[i + 2:i + 5]]
                    col = QColor(*[min(255, v) for v in vals])
                    i += 4
                if n == 38:
                    self.fg = col
                else:
                    self.bg = col
            i += 1


def parse(screen: str):
    """-> list of lines, each a list of (text, style-snapshot) runs."""
    st = Style()
    lines = []
    for raw in screen.split("\n"):
        raw = OTHER_ESC_RE.sub("", raw)
        runs = []
        pos = 0
        for m in SGR_RE.finditer(raw):
            if m.start() > pos:
                runs.append((raw[pos:m.start()], st.key()))
            st.apply(m.group(1))
            pos = m.end()
        if pos < len(raw):
            runs.append((raw[pos:], st.key()))
        lines.append(runs)
    while lines and not any(t.strip() for t, _ in lines[-1]):
        lines.pop()
    return lines


_font_cache: dict[float, tuple[QFont, QFontMetricsF]] = {}


def mono(px: float) -> tuple[QFont, QFontMetricsF]:
    if px not in _font_cache:
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        for fam in ("JetBrains Mono", "DejaVu Sans Mono", "Menlo", "Monospace"):
            if fam in QFontDatabase.families():
                f = QFont(fam)
                break
        f.setPixelSize(int(px))
        f.setStyleHint(QFont.Monospace)
        f.setHintingPreference(QFont.PreferNoHinting)
        _font_cache[px] = (f, QFontMetricsF(f))
    return _font_cache[px]


def render(screen: str, cols: int, rows: int, px: float = 12, bottom_rows: int | None = None) -> QImage:
    """Render the screen; if bottom_rows is set, crop to the last N non-empty rows."""
    font, fm = mono(px)
    cw = fm.horizontalAdvance("M")
    lh = fm.height()
    lines = parse(screen)
    if bottom_rows and len(lines) > bottom_rows:
        lines = lines[-bottom_rows:]
    nrows = max(len(lines), 1) if bottom_rows else max(rows, len(lines), 1)
    used = max((len("".join(t for t, _ in ln).rstrip()) for ln in lines), default=0)
    cols = min(cols, max(used, 50))  # crop the empty right side
    img = QImage(int(cols * cw) + 8, int(nrows * lh) + 8, QImage.Format_ARGB32_Premultiplied)
    bg = QColor(theme.TERM_BG)
    img.fill(bg)
    p = QPainter(img)
    p.setRenderHint(QPainter.TextAntialiasing)
    bold = QFont(font)
    bold.setBold(True)
    default_fg = QColor(theme.TERM_FG)
    y0 = 4
    for r, runs in enumerate(lines):
        col = 0
        y = y0 + r * lh
        for text, (fg, bgc, b, dim, rev, ital) in runs:
            if not text:
                continue
            fgc = QColor.fromRgba(fg) if fg is not None else default_fg
            bgq = QColor.fromRgba(bgc) if bgc is not None else None
            if rev:
                fgc, bgq = (bgq or bg), fgc
            w = len(text) * cw
            x = 4 + col * cw
            if bgq is not None:
                p.fillRect(int(x), int(y), int(w + 1), int(lh), bgq)
            if text.strip():
                if dim:
                    fgc = QColor(fgc)
                    fgc.setAlphaF(0.55)
                p.setPen(fgc)
                p.setFont(bold if b else font)
                p.drawText(int(x), int(y + fm.ascent()), text)
            col += len(text)
    p.end()
    return img
