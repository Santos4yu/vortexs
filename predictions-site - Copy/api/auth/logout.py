"""
Vercel serverless function: GET /api/auth/logout
Clears the session cookie and sends the user back to the homepage.
Doesn't need to contact Discord -- the cookie IS the session, so deleting
it client-side (via Max-Age=0) is a complete logout.
"""

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth_core import SESSION_COOKIE_NAME  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
        )
        self.end_headers()
