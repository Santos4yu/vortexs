"""
Vercel serverless function: GET /api/auth/me
Reads the session cookie and reports whether it's a valid, unexpired,
correctly-signed session. The frontend calls this on load to decide
whether to show the research UI or the login gate.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth_core import (  # noqa: E402
    verify_session_token, parse_cookie_header, SESSION_COOKIE_NAME,
    FREE_BETA_MODE, _beta_payload,
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if FREE_BETA_MODE:
            payload = _beta_payload()
        else:
            cookie_header = self.headers.get("Cookie", "")
            token = parse_cookie_header(cookie_header, SESSION_COOKIE_NAME)
            payload = verify_session_token(token)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        if not payload:
            self.wfile.write(json.dumps({"authenticated": False}).encode("utf-8"))
            return

        self.wfile.write(json.dumps({
            "authenticated": True,
            "username": payload.get("username"),
            "hasPremium": payload.get("has_premium", False),
            "hasTester": payload.get("has_tester", False),
        }).encode("utf-8"))
