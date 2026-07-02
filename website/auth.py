"""Discord OAuth2 flow — login, callback, role check."""

import os
import secrets
import logging
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("vortex.auth")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")

DISCORD_API = "https://discord.com/api/v10"

# Shared client to avoid SSL/TLS issues
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client
PREMIUM_ROLE_ID = int(os.getenv("PREMIUM_ROLE_ID", "1516353685402292274"))
TESTER_ROLE_ID = int(os.getenv("TESTER_ROLE_ID", "1515612947110690846"))
VORTEX_GUILD = 1515224924267216926


async def get_oauth_url(state: str) -> str:
    return (
        f"{DISCORD_API}/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify+guilds+guilds.members.read"
        f"&state={state}"
    )


async def exchange_code(code: str) -> Optional[dict]:
    """Exchange auth code for access + refresh tokens."""
    if not CLIENT_SECRET:
        log.error("DISCORD_CLIENT_SECRET is not set in .env")
        return None
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    c = await _get_client()
    r = await c.post(f"{DISCORD_API}/oauth2/token", data=data, headers=headers)
    if r.is_error:
        log.error("Discord token exchange failed: %s %s", r.status_code, r.text[:500])
        return None
    return r.json()


async def get_identity(access_token: str) -> dict:
    """Fetch user's Discord identity."""
    headers = {"Authorization": f"Bearer {access_token}"}
    c = await _get_client()
    r = await c.get(f"{DISCORD_API}/users/@me", headers=headers)
    return r.json()


async def get_guild_member(access_token: str, user_id: str) -> Optional[dict]:
    """Fetch user's membership & roles in the VORTEX guild using the bot token.

    The user OAuth token can't reliably call /guilds/{id}/member.
    The bot token (with Server Members Intent) can.
    """
    c = await _get_client()

    # Use bot token to fetch the member — reliable and includes roles
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    r = await c.get(
        f"{DISCORD_API}/guilds/{VORTEX_GUILD}/members/{user_id}",
        headers=headers,
    )
    log.info("Guild member fetch (bot token): user=%s status=%s", user_id, r.status_code)
    if not r.is_error:
        data = r.json()
        log.info("Member roles: %s", data.get("roles", []))
        return data

    log.warning("Bot token member fetch failed: %s %s", r.status_code, r.text[:300])

    # Fallback: use user token to at least check guild membership
    user_headers = {"Authorization": f"Bearer {access_token}"}
    r2 = await c.get(f"{DISCORD_API}/users/@me/guilds", headers=user_headers)
    if r2.is_error:
        log.error("Failed to fetch user guilds: %s", r2.status_code)
        return None

    guilds = r2.json()
    in_guild = any(g.get("id") == str(VORTEX_GUILD) for g in guilds)
    if not in_guild:
        log.warning("User %s is NOT in guild %s", user_id, VORTEX_GUILD)
        return None

    log.info("User %s is in guild %s (role check bypassed)", user_id, VORTEX_GUILD)
    return {"roles": []}


def has_access(member: dict) -> bool:
    """Check if the user has Premium or Tester role."""
    if not member:
        log.info("has_access: no member data")
        return False
    roles = [str(r) for r in member.get("roles", [])]
    premium = str(PREMIUM_ROLE_ID) in roles
    tester = str(TESTER_ROLE_ID) in roles
    log.info("Role check: roles=%s premium=%s tester=%s", roles, premium, tester)
    return premium or tester
