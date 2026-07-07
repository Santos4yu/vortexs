"""
Vercel serverless function: verifies the VORTEX V2 admin PIN and, on
success, issues the signed admin-session cookie v2/board/admin_auth.py
defines. This is the server-side half of the disguised Settings button --
the PIN itself is never checked in the browser, only here.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v2.board import admin_auth, store  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "Invalid request body"})

        locked, seconds_left = store.is_pin_locked_out()
        if locked:
            return self._send(429, {"error": f"Too many attempts. Try again in {seconds_left // 60 + 1} min."})

        pin = str(body.get("pin", ""))
        if not admin_auth.check_pin(pin):
            store.record_pin_failure()
            return self._send(401, {"error": "Incorrect PIN"})

        store.clear_pin_failures()
        token = admin_auth.create_admin_token()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Set-Cookie",
            f"{admin_auth.ADMIN_COOKIE_NAME}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; "
            f"Max-Age={admin_auth.ADMIN_TTL_SEC}",
        )
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
