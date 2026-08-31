"""
Vercel serverless function: GET /api/auth/login
Redirects to Discord's OAuth authorize page. A random CSRF state value is
set as a short-lived cookie (not a server-side session store -- there is
none here) and re-checked against Discord's callback ?state= param.
"""

import secrets
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth_core import get_oauth_url  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = secrets.token_urlsafe(24)
        url = get_oauth_url(state)

        self.send_response(302)
        self.send_header("Location", url)
        self.send_header(
            "Set-Cookie",
            f"vortex_oauth_state={state}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax",
        )
        self.end_headers()
