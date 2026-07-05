"""
Vercel serverless function: GET /api/auth/callback
Discord redirects here after the user approves the OAuth request. Exchanges
the code, fetches identity + guild role membership, and either issues a
signed session cookie (access granted) or sends them back with a reason
(denied -- authenticated with Discord fine, just doesn't have the paid role).
"""

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth_core import (  # noqa: E402
    exchange_code, get_identity, get_guild_member, has_access,
    create_session_token, parse_cookie_header, SESSION_COOKIE_NAME, SESSION_TTL_SEC,
    PREMIUM_ROLE_ID, TESTER_ROLE_ID,
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        code = (qs.get("code", [""])[0]).strip()
        state = (qs.get("state", [""])[0]).strip()

        cookie_header = self.headers.get("Cookie", "")
        saved_state = parse_cookie_header(cookie_header, "vortex_oauth_state")

        clear_state_cookie = "vortex_oauth_state=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"

        if not code or not state or state != saved_state:
            return self._redirect_home("error", extra_headers=[("Set-Cookie", clear_state_cookie)])

        token_data = exchange_code(code)
        if not token_data or not token_data.get("access_token"):
            return self._redirect_home("error", extra_headers=[("Set-Cookie", clear_state_cookie)])

        access_token = token_data["access_token"]
        user = get_identity(access_token)
        if not user.get("id"):
            return self._redirect_home("error", extra_headers=[("Set-Cookie", clear_state_cookie)])

        member = get_guild_member(access_token, user["id"])
        if not member or not has_access(member):
            return self._redirect_home("denied", extra_headers=[("Set-Cookie", clear_state_cookie)])

        roles = {str(r) for r in (member.get("roles") or [])}
        session_token = create_session_token(
            user,
            has_premium=str(PREMIUM_ROLE_ID) in roles,
            has_tester=str(TESTER_ROLE_ID) in roles,
        )
        session_cookie = (
            f"{SESSION_COOKIE_NAME}={session_token}; Path=/; Max-Age={SESSION_TTL_SEC}; "
            f"HttpOnly; Secure; SameSite=Lax"
        )
        self._redirect_home("success", extra_headers=[
            ("Set-Cookie", clear_state_cookie),
            ("Set-Cookie", session_cookie),
        ])

    def _redirect_home(self, result: str, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", f"/?auth={result}")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
