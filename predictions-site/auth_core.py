"""
Vortex — Discord OAuth + role-gated access (shared by every platform wrapper)
=============================================================================
Ports the proven OAuth/role-check logic from the old website/auth.py (a
since-abandoned FastAPI attempt at this same site) onto Vercel's stateless
serverless functions. The old app used server-side session middleware,
which doesn't fit a serverless model (no persistent process to hold
session state) -- this version issues a signed, stateless session cookie
instead: the cookie itself carries the user's identity + an expiry,
authenticated with an HMAC signature (WEB_SECRET_KEY) so it can't be
forged, and needs no database or server-side session store to verify.

Revocation model: removing someone's Discord role doesn't invalidate an
already-issued cookie instantly -- it invalidates on next re-check, which
happens whenever the cookie expires (SESSION_TTL_SEC below). That's an
intentional tradeoff for staying database-free; shorten SESSION_TTL_SEC
for tighter revocation at the cost of more Discord API calls.

Local dev: reads the repo's .env (same file the bot uses) via python-dotenv.
Production (Vercel): set these as real Environment Variables in the project
dashboard -- Vercel doesn't read a committed .env file (and .env is
gitignored anyway, so it wouldn't be there to read).
"""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
SESSION_SECRET = os.getenv("WEB_SECRET_KEY", "")

VORTEX_GUILD = int(os.getenv("VORTEX_GUILD_ID", "1515224924267216926"))
# Real paid-access role for the Vortex Discord community (as given).
PREMIUM_ROLE_ID = int(os.getenv("PREMIUM_ROLE_ID", "1523159038936748153"))
# No hardcoded fallback here on purpose -- the old test project's "tester"
# role ID used to default here, which silently let anyone still holding
# that leftover test role in without the real paid role. TESTER_ROLE_ID is
# 0 (never matches) unless you deliberately set it via a Vercel env var.
TESTER_ROLE_ID = int(os.getenv("TESTER_ROLE_ID", "0"))

DISCORD_API = "https://discord.com/api/v10"
SESSION_COOKIE_NAME = "vortex_session"
SESSION_TTL_SEC = 24 * 3600  # re-checks Discord role at most once a day per user

# The free-beta bypass (FREE_BETA_MODE env var) was removed when the site
# went paid: access now ALWAYS requires a Discord login + the premium role,
# and no environment variable can silently reopen the free path. To run
# another free trial someday, restore the gate short-circuits from git
# history (commit that removed them) rather than reintroducing an env flag.

# ── OAuth flow (sync/requests -- matches this site's existing style) ────────

def get_oauth_url(state: str) -> str:
    return (
        f"{DISCORD_API}/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify+guilds+guilds.members.read"
        f"&state={state}"
    )


def exchange_code(code: str) -> dict | None:
    """Exchange the OAuth authorization code for an access token."""
    if not CLIENT_SECRET:
        return None
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    r = requests.post(
        f"{DISCORD_API}/oauth2/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if not r.ok:
        return None
    return r.json()


def get_identity(access_token: str) -> dict:
    """The logged-in user's own Discord identity (id, username, avatar)."""
    r = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    return r.json() if r.ok else {}


def get_guild_member(access_token: str, user_id: str) -> dict | None:
    """
    This user's membership + roles in the Vortex Discord guild. The BOT
    token (not the user's OAuth token) is what's reliably allowed to read
    another member's roles; falls back to a membership-only check via the
    user's own token (Discord's /users/@me/guilds) if the bot lookup fails,
    matching the guild but with no role data (has_access will read false).
    """
    if BOT_TOKEN:
        r = requests.get(
            f"{DISCORD_API}/guilds/{VORTEX_GUILD}/members/{user_id}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            timeout=15,
        )
        if r.ok:
            return r.json()

    r2 = requests.get(
        f"{DISCORD_API}/users/@me/guilds",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if not r2.ok:
        return None
    guilds = r2.json()
    if not any(g.get("id") == str(VORTEX_GUILD) for g in guilds):
        return None
    return {"roles": []}  # in the guild, but role list unknown via this path


def has_access(member: dict | None) -> bool:
    if not member:
        return False
    roles = {str(r) for r in (member.get("roles") or [])}
    return str(PREMIUM_ROLE_ID) in roles or str(TESTER_ROLE_ID) in roles


# ── Stateless signed session cookie (replaces server-side session middleware) ─

def _sign(payload_b64: str) -> str:
    if not SESSION_SECRET:
        raise RuntimeError("WEB_SECRET_KEY is not set -- cannot sign session cookies")
    mac = hmac.new(SESSION_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("utf-8").rstrip("=")


def create_session_token(user: dict, has_premium: bool, has_tester: bool) -> str:
    payload = {
        "id": user.get("id"),
        "username": user.get("username") or user.get("global_name") or "Member",
        "avatar": user.get("avatar"),
        "has_premium": has_premium,
        "has_tester": has_tester,
        "exp": int(time.time()) + SESSION_TTL_SEC,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_session_token(token: str) -> dict | None:
    """Returns the session payload if the signature is valid and it hasn't
    expired, else None. No database/lookup needed -- the token IS the proof."""
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    try:
        expected_sig = _sign(payload_b64)
    except RuntimeError:
        return None
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def parse_cookie_header(cookie_header: str, name: str) -> str:
    """Minimal cookie-header parser -- no external dependency needed for
    reading a single named cookie out of the raw Cookie request header."""
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1:]
    return ""


def session_from_request_headers(headers) -> dict | None:
    """Convenience for API handlers: pass BaseHTTPRequestHandler.headers,
    get back the verified session payload or None. Cheap -- just checks the
    cookie's signature/expiry, no network call. Used for low-value endpoints
    (player-name autocomplete) where re-verifying Discord on every keystroke
    would be wasteful and risks hitting Discord's rate limits for no real
    security benefit."""
    cookie_header = headers.get("Cookie", "")
    token = parse_cookie_header(cookie_header, SESSION_COOKIE_NAME)
    return verify_session_token(token)


def check_live_access(user_id: str) -> bool:
    """
    Re-checks role membership directly against Discord's API right now --
    no caching, no trusting the session cookie's claims. This is what makes
    revocation instant: removing someone's Discord role cuts their access on
    their very next request, instead of whenever their cached cookie
    happens to expire (previously up to 24h later).
    """
    if not BOT_TOKEN or not user_id:
        return False
    try:
        r = requests.get(
            f"{DISCORD_API}/guilds/{VORTEX_GUILD}/members/{user_id}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            timeout=8,
        )
    except requests.RequestException:
        # Treat a Discord API hiccup as "can't confirm access" rather than
        # silently letting a possibly-revoked user through.
        return False
    if not r.ok:
        return False
    return has_access(r.json())


def session_with_live_access(headers) -> dict | None:
    """
    Like session_from_request_headers, but also re-verifies the Discord
    role LIVE (no cache). Use this for the actual paid data endpoints
    (prediction, team-insights) so role removal takes effect immediately,
    not "eventually, once their cookie expires".
    """
    payload = session_from_request_headers(headers)
    if not payload:
        return None
    if not check_live_access(payload.get("id", "")):
        return None
    return payload
