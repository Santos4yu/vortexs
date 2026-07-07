"""
Vercel serverless function: admin-only view/swap of the live Odds API key.

GET  -- tests whatever key is currently active (from the live store, or the
        .env fallback) and reports remaining credits, without ever
        returning the key value itself.
POST -- tests a NEW candidate key first; only saves it to the live store
        (v2/board/store.py) if the test call succeeds. A rejected key never
        overwrites a working one.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v2.board import admin_auth, odds_client, store  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        if not admin_auth.is_admin_request(self.headers):
            return self._send(401, {"error": "Admin session required"})
        current_key = store.get_odds_api_key()
        if not current_key:
            return self._send(200, {"keySet": False})
        result = odds_client.test_key(current_key)
        result["keySet"] = True
        return self._send(200, result)

    def do_POST(self):
        if not admin_auth.is_admin_request(self.headers):
            return self._send(401, {"error": "Admin session required"})

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "Invalid request body"})

        new_key = (body.get("key") or "").strip()
        if not new_key:
            return self._send(400, {"error": "No key provided"})

        result = odds_client.test_key(new_key)
        if not result.get("valid"):
            return self._send(400, {"saved": False, **result})

        store.set_odds_api_key(new_key)
        result["saved"] = True
        return self._send(200, result)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
