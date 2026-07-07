"""
Small key-value store for VORTEX V2, backed by Upstash Redis's REST API
(plain HTTP, no Redis client library needed -- works from any Vercel
serverless function without extra deploy weight).

This is what makes the odds-API-key swap and the props board actually
"live": both are stored here instead of in a static .env/Vercel env var, so
a change takes effect on the very next request -- no redeploy, no restart.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

KV_URL = os.getenv("KV_REST_API_URL", "")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN", "")
TIMEOUT = 10

ODDS_KEY_STORAGE_KEY = "vortex_v2:odds_api_key"
BOARD_STORAGE_KEY = "vortex_v2:board_today"


def _headers() -> dict:
    return {"Authorization": f"Bearer {KV_TOKEN}"}


def get(key: str) -> str | None:
    if not KV_URL or not KV_TOKEN:
        return None
    r = requests.get(f"{KV_URL}/get/{key}", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("result")


def set(key: str, value: str) -> bool:
    if not KV_URL or not KV_TOKEN:
        return False
    r = requests.post(f"{KV_URL}/set/{key}", data=value.encode("utf-8"), headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("result") == "OK"


def get_odds_api_key() -> str:
    """The live-swappable odds key. Falls back to .env's ODDS_API_KEY_V2 if
    nothing has been set in the store yet (e.g. right after first deploy)."""
    return get(ODDS_KEY_STORAGE_KEY) or os.getenv("ODDS_API_KEY_V2", "")


def set_odds_api_key(new_key: str) -> bool:
    return set(ODDS_KEY_STORAGE_KEY, new_key)


# ── PIN brute-force protection ──────────────────────────────────────────────
# A 4-digit PIN is only 10,000 combinations -- worthless without a rate
# limit. Tracked globally (not per-IP) in the shared store since this is a
# single-admin panel, not a multi-user login.
PIN_FAIL_COUNT_KEY = "vortex_v2:admin_pin_fail_count"
PIN_LOCK_UNTIL_KEY = "vortex_v2:admin_pin_lock_until"
MAX_PIN_ATTEMPTS = 5
LOCKOUT_SEC = 15 * 60


def is_pin_locked_out() -> tuple[bool, int]:
    import time
    lock_until = get(PIN_LOCK_UNTIL_KEY)
    if lock_until and float(lock_until) > time.time():
        return True, int(float(lock_until) - time.time())
    return False, 0


def record_pin_failure() -> None:
    import time
    count = int(get(PIN_FAIL_COUNT_KEY) or 0) + 1
    set(PIN_FAIL_COUNT_KEY, str(count))
    if count >= MAX_PIN_ATTEMPTS:
        set(PIN_LOCK_UNTIL_KEY, str(time.time() + LOCKOUT_SEC))
        set(PIN_FAIL_COUNT_KEY, "0")


def clear_pin_failures() -> None:
    set(PIN_FAIL_COUNT_KEY, "0")
