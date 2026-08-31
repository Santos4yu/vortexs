"""
Vercel serverless function for the Attack Board (today's starting-pitcher
matchup difficulty ranking).

Mirrors api/prediction.py's platform-wrapper pattern -- all logic lives in
prediction_core.compute_slate() so it stays in sync with the Discord bot's
/slate command (same underlying stats_mlb data).
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_core import compute_slate, compute_tool  # noqa: E402
from auth_core import session_with_live_access  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        if not session_with_live_access(self.headers):
            return self._send(401, {"error": "Sign in with Discord to use live research.", "authRequired": True})

        try:
            tool = parse_qs(urlparse(self.path).query).get("tool", ["attack"])[0]
            result = compute_slate() if tool == "attack" else compute_tool(tool)
        except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the client
            return self._send(500, {"error": f"Slate lookup failed: {exc}"})

        return self._send(200, result)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
