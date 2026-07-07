"""
Vercel serverless function: public read of the last VORTEX V2 board scan.
Gated the same way as the rest of the site's paid features
(session_with_live_access). Just reads whatever api/v2-admin-scan.py last
wrote to the store -- never triggers a scan itself, so browsing this tab
never spends odds credits.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth_core import session_with_live_access  # noqa: E402
from v2.board import store  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        if not session_with_live_access(self.headers):
            return self._send(401, {"error": "Sign in with Discord to use live research.", "authRequired": True})

        raw = store.get(store.BOARD_STORAGE_KEY)
        if not raw:
            return self._send(200, {"date": None, "props": []})
        try:
            return self._send(200, json.loads(raw))
        except json.JSONDecodeError:
            return self._send(200, {"date": None, "props": []})

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
