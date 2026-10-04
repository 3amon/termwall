#!/usr/bin/env python3
"""Render the overlay from fixture data to a PNG, without a display.

    uv run python demo/render_overlay.py docs/overlay.png
    uv run python demo/render_overlay.py /tmp/recent.png --mode recent

This builds the real `Overlay` widget on Qt's offscreen platform, feeds it the
invented sessions in `demo/fixtures.py` through `Store.ingest`, and grabs the
widget. It does not start the daemon, open the control socket, launch a
terminal, or contact any host, and it keeps its config/state in a temporary
directory, so it is safe to run next to a live termwall.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", help="PNG to write")
    ap.add_argument("--mode", choices=["live", "recent"], default="live")
    ap.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT (default 1920x1080)")
    ap.add_argument("--backdrop", default="#39404f",
                    help="colour standing in for the desktop behind the translucent overlay; "
                         "'none' keeps the alpha channel")
    args = ap.parse_args()
    width, height = (int(v) for v in args.size.lower().split("x"))

    with tempfile.TemporaryDirectory(prefix="termwall-demo-") as tmp:
        # termwall.config reads these at import time: set them first
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
        os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        sys.path.insert(0, str(HERE))
        sys.path.insert(0, str(HERE.parent / "src"))

        import tomllib

        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QApplication

        import fixtures
        from termwall import config
        from termwall.model import Store
        from termwall.overlay import Overlay

        defaults = tomllib.loads(config.DEFAULT_CONFIG)
        cfg = config.Config(
            hosts=[config.Host("workstation"), config.Host("buildbox", "buildbox")],
            **{k: dict(defaults[k]) for k in ("agents", "terminal", "ui", "notify", "web")})
        app = QApplication(sys.argv[:1])
        store = Store(cfg)
        fixtures.load(store)

        overlay = Overlay(store)
        overlay.mode = args.mode
        overlay.resize(width, height)
        overlay.refresh(preselect=True)
        overlay.appear_t = 1.0
        out = os.path.abspath(args.out)
        shot = overlay.grab().toImage()
        if args.backdrop != "none":
            # on a desktop the overlay is drawn over whatever is behind it; use a flat colour
            canvas = QImage(shot.size(), QImage.Format_RGB32)
            canvas.fill(QColor(args.backdrop))
            painter = QPainter(canvas)
            painter.drawImage(0, 0, shot)
            painter.end()
            shot = canvas
        ok = shot.save(out)
        del overlay, app
    print(out if ok else f"could not write {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
