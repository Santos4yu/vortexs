"""
Live, "as of right now" feature construction for a specific player -- the
deployed counterpart to v2/training/build_features.py.

Reuses stats_mlb's own request/cache machinery directly (so a live gamelog
fetch inherits its 14h "volatile" TTL, correct for in-progress-season data --
unlike v2/training/fetch_gamelogs.py's indefinite cache, which is only valid
for a finished, immutable past season). Reuses
v2.training.build_features.build_point_in_time_features for the actual
rolling-window math -- that function is pure/stateless, so sharing it here
keeps training and inference computing L5/L10/L20 identically without
duplicating the arithmetic.
"""
import sys
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import stats_mlb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.training.build_features import build_point_in_time_features  # noqa: E402


def fetch_current_season_gamelog(player_id: int) -> list:
    """This season's batting gamelog for player_id, as of today. Same
    cache_key convention stats_mlb.get_historical_splits uses (gamelog_
    prefix -> 14h TTL), so it stays fresh through a game day without
    re-fetching on every call."""
    today = _date.today().isoformat()
    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "gameLog", "group": "hitting", "season": stats_mlb.SEASON, "sportId": 1},
        cache_key=f"gamelog_hit_{player_id}_{stats_mlb.SEASON}_{today}",
    )
    raw_splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    games = [
        {
            "date": s.get("date"),
            "is_home": bool(s.get("isHome")),
            "opponent_id": (s.get("opponent") or {}).get("id"),
            "stat": s.get("stat") or {},
        }
        for s in raw_splits
        if s.get("date")
    ]
    games.sort(key=lambda g: g["date"])
    return games


def build_live_features(player_id: int, is_home_today: bool) -> dict | None:
    """Returns None if the player doesn't have enough of a game log yet
    this season (fewer than 5 games) -- same guard build_features.py uses
    for training rows, applied here so early-season predictions aren't
    made off a near-empty sample."""
    games = fetch_current_season_gamelog(player_id)
    today = _date.today().isoformat()
    return build_point_in_time_features(games, today, is_home_today)
