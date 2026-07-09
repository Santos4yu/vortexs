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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import MARKET_FOR_STAT  # noqa: E402

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "baseball_mlb"
# Every stat_type's market gets batched into ONE call per event (see
# fetch_event_props) -- with 10 stat_types now instead of 3, a fully-posted
# event can cost up to 10 credits instead of 3. build_board.py's shortlist
# size is the real lever for controlling total spend, not this list.
MARKETS = list(MARKET_FOR_STAT.values())
TIMEOUT = 15

# Sharp reference book -- never surfaced as a bettable price, only used to
# de-vig a true hit probability (see backend/update_board.py's identical
# SHARP_BOOK/PREFERRED_BOOKS split, which this mirrors).
SHARP_BOOK = "pinnacle"
# The only books VORTEX V2 is allowed to actually recommend a bet on --
# the user only plays DraftKings/Underdog/PrizePicks. build_board.py's
# attach_real_odds() only accepts a candidate whose best price comes from
# one of these.
PREFERRED_BOOKS = ["underdogfantasy", "underdog", "draftkings", "prizepicks"]
# Requesting a handful of extra mainstream two-way books alongside the
# preferred ones costs nothing extra (still one fetch_event_props call per
# event) and gives consensus_no_vig_prob a real two-way market to de-vig
# against even on nights Pinnacle doesn't carry this exact line -- DFS
# platforms alone are usually single-sided and can't de-vig themselves.
TARGET_BOOKS = PREFERRED_BOOKS + ["fanduel", "betmgm", "espnbet", "betrivers", SHARP_BOOK]

_SESSION = requests.Session()


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
                "bookmakers": ",".join(TARGET_BOOKS),
                "markets": ",".join(MARKETS), "oddsFormat": "american"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    print(f"  [odds api] event {event_id[:8]}... -- "
          f"{r.headers.get('x-requests-remaining', '?')} credits remaining")
    return r.json()
