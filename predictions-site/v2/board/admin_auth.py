"""
Admin-session signing for VORTEX V2's hidden admin panel.

Same stateless, HMAC-signed-cookie approach predictions-site/auth_core.py
already uses for Discord login (no database/session store needed -- the
token itself carries proof of a correct PIN + an expiry, and can't be forged
without WEB_SECRET_KEY). Kept separate from auth_core.py's Discord session
token on purpose: this is a distinct, narrower credential (proves "knows the
admin PIN," not "is a specific Discord user") gating a distinct, more
dangerous set of actions (spending odds credits, changing the live API key).
"""
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

SESSION_SECRET = os.getenv("WEB_SECRET_KEY", "")
ADMIN_PIN = os.getenv("ADMIN_PIN", "")
ADMIN_COOKIE_NAME = "vortex_v2_admin"
ADMIN_TTL_SEC = 4 * 3600  # re-enter the PIN every 4 hours


def _sign(payload_b64: str) -> str:
    if not SESSION_SECRET:
        raise RuntimeError("WEB_SECRET_KEY is not set -- cannot sign admin tokens")
    mac = hmac.new(SESSION_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("utf-8").rstrip("=")


def check_pin(submitted_pin: str) -> bool:
    if not ADMIN_PIN:
        return False
    return hmac.compare_digest(str(submitted_pin), str(ADMIN_PIN))


def create_admin_token() -> str:
    payload = {"admin": True, "exp": int(time.time()) + ADMIN_TTL_SEC}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_admin_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    payload_b64, sig = token.rsplit(".", 1)
    try:
        expected_sig = _sign(payload_b64)
    except RuntimeError:
        return False
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return False
    return bool(payload.get("admin")) and payload.get("exp", 0) >= time.time()


def parse_cookie_header(cookie_header: str, name: str) -> str:
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1:]
    return ""


def is_admin_request(headers) -> bool:
    cookie_header = headers.get("Cookie", "") or headers.get("cookie", "")
    token = parse_cookie_header(cookie_header, ADMIN_COOKIE_NAME)
    return verify_admin_token(token)
