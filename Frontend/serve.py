#!/usr/bin/env python3
"""
serve.py — local Frontend file server with no-cache headers.

Why this exists: ``python3 -m http.server`` does not emit a
``Cache-Control`` header, so browsers cache JS/CSS modules
aggressively. After an edit to e.g. ``notifications.js``, a normal
reload (``Cmd+R``) serves the cached OLD copy — the new code only
appears on a hard refresh (``Cmd+Shift+R``). That is the recurring
"trouble reloading" symptom.

Fix: every response carries
``Cache-Control: no-cache, no-store, must-revalidate`` (plus the
legacy ``Pragma`` / ``Expires`` headers for older intermediaries).
Reloads always re-fetch. Localhost bandwidth cost is negligible —
the files are tiny and the LAN never sees them.

Usage:  python3 serve.py [port]   (default 3000)
"""
from __future__ import annotations

import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that disables browser caching on every
    response. Drop-in replacement for ``python -m http.server``."""

    def end_headers(self):
        # Belt-and-suspenders: ``Cache-Control`` is the modern directive,
        # ``Pragma`` + ``Expires`` cover older intermediaries / odd
        # browsers that ignore Cache-Control. Together they make every
        # fetch unconditional.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    # Bind 0.0.0.0 to match start-lan.sh's LAN-accessible posture
    # (so a phone on the same Wi-Fi can reach the dashboard). Switch
    # to "127.0.0.1" if you want localhost-only.
    server = ThreadingHTTPServer(("0.0.0.0", port), NoCacheHandler)
    print(f"Serving Frontend with no-cache headers on http://127.0.0.1:{port}/")
    print("(Reloads always re-fetch — no stale browser cache.)")
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
