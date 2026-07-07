"""
Real MLB player-prop odds fetching for VORTEX V2, using its own dedicated
Odds API key so it never competes with the Discord bot's budget. Mirrors
backend/update_board.py's fetch_all_markets_batched pattern (batch every
market into ONE call per event, not one call per market) but the
cost-control decision of WHICH events are worth paying for lives in
build_board.py, not here -- list_events() is a free call, fetch_event_props()
is the one that costs credits and should only be called for a shortlist.

The key itself is read fresh from v2/board/store.py (the live Upstash-backed
store) on every call, not frozen into a module-level constant at import time
-- that's the whole point of the admin panel's key-swap: a new key takes
effect on the very next request, no redeploy or restart needed.
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store import get_odds_api_key  # noqa: E402

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "baseball_mlb"
MARKETS = ["batter_hits", "batter_total_bases", "batter_home_runs"]
TIMEOUT = 15

_SESSION = requests.Session()

# stat_type (v2/common/stat_types.py) -> Odds API market key
MARKET_FOR_STAT = {
    "hits": "batter_hits",
    "total_bases": "batter_total_bases",
    "home_runs": "batter_home_runs",
}


def test_key(candidate_key: str) -> dict:
    """Validates a NOT-YET-SAVED key against the free /sports endpoint (does
    not cost credits) -- used by the admin key-swap UI so a bad key is
    rejected before it overwrites the working one."""
    try:
        r = _SESSION.get(f"{BASE_URL}/sports", params={"apiKey": candidate_key}, timeout=TIMEOUT)
        if r.status_code == 401:
            return {"valid": False, "error": "Key rejected (401 Unauthorized)"}
        r.raise_for_status()
        return {
            "valid": True,
            "requests_remaining": r.headers.get("x-requests-remaining", "?"),
            "requests_used": r.headers.get("x-requests-used", "?"),
        }
    except requests.RequestException as exc:
        return {"valid": False, "error": str(exc)}


def list_events() -> list:
    """Free call (does not consume API credits). Returns today's/upcoming
    MLB events: [{id, home_team, away_team, commence_time}, ...]."""
    key = get_odds_api_key()
    if not key:
        return []
    r = _SESSION.get(f"{BASE_URL}/sports/{SPORT_KEY}/events",
                      params={"apiKey": key, "dateFormat": "iso"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_event_props(event_id: str) -> dict:
    """Costs credits (up to len(MARKETS) x 1 region per event -- see
    v2/board/build_board.py's shortlist logic for why this is only called
    for a small, pre-filtered set of events, not the whole day's slate).
    Prints the remaining-credits header after every call so a runaway
    shortlist is visible immediately, not just at month's end."""
    key = get_odds_api_key()
    r = _SESSION.get(
        f"{BASE_URL}/sports/{SPORT_KEY}/events/{event_id}/odds",
        params={"apiKey": key, "regions": "us",
                "markets": ",".join(MARKETS), "oddsFormat": "american"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    print(f"  [odds api] event {event_id[:8]}... -- "
          f"{r.headers.get('x-requests-remaining', '?')} credits remaining")
    return r.json()
