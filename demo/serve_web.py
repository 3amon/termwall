#!/usr/bin/env python3
"""Serve the phone app with fixture data on 127.0.0.1, for screenshots and UI work.

    uv run python demo/serve_web.py --port 8791
    # then open the printed URL (it carries a throwaway pairing token)

The real `webserver.make_app()` is used, with two changes: host connections are
replaced by a no-op (no agent process is started, nothing is reached over ssh)
and the store is filled from `demo/fixtures.py`. Config, pairing token and the
generated push key live in a temporary directory that is removed on exit.
Actions that need a host (open a terminal, reply, kill, search) answer
"host is offline" in this mode; the wall itself is the real page.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="termwall-demo-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
        os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
        sys.path.insert(0, str(HERE))
        sys.path.insert(0, str(HERE.parent / "src"))

        from aiohttp import web

        import fixtures
        from termwall import config, webserver

        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config.CONFIG_PATH.write_text(fixtures.CONFIG_TOML)

        async def no_hosts(self):          # never start an agent or an ssh connection
            await asyncio.sleep(3600 * 24)
        webserver.Host.run = no_hosts

        app = webserver.make_app()
        fixtures.load(app["store"])
        print(f"http://127.0.0.1:{args.port}/#token={app['token']}", flush=True)
        web.run_app(app, host="127.0.0.1", port=args.port, print=None,
                    access_log_class=webserver.PathOnlyLog)
    return 0


if __name__ == "__main__":
    sys.exit(main())
