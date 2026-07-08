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
from v2.common.stat_types import BATTER_RAW_FIELDS, PITCHER_RAW_FIELDS  # noqa: E402


def fetch_current_season_gamelog(player_id: int, group: str) -> list:
    """This season's gamelog for player_id, as of today. Same cache_key
    convention stats_mlb.get_historical_splits uses (gamelog_ prefix -> 14h
    TTL), so it stays fresh through a game day without re-fetching on every
    call. Public because v2/board/traps.py reads the same gamelog for streak
    detection -- its second call is a disk-cache hit, not a second fetch."""
    today = _date.today().isoformat()
    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "gameLog", "group": group, "season": stats_mlb.SEASON, "sportId": 1},
        cache_key=f"gamelog_{group}_{player_id}_{stats_mlb.SEASON}_{today}",
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
    """Batter version. Returns None if the player doesn't have enough of a
    game log yet this season (fewer than 5 games)."""
    games = fetch_current_season_gamelog(player_id, "hitting")
    today = _date.today().isoformat()
    return build_point_in_time_features(games, today, is_home_today, BATTER_RAW_FIELDS)


def build_live_pitcher_features(player_id: int, is_home_today: bool) -> dict | None:
    """Pitcher version -- same idea, pitching gamelog + PITCHER_RAW_FIELDS.
    Filtered to the pitcher's own STARTS (see v2/training/dataset.py's
    build_pitcher_rows_for_season for why) so the rolling-window history
    matches what the model was trained on -- a start's worth of strikeouts/
    outs/hits-allowed history, not diluted by any relief innings."""
    games = fetch_current_season_gamelog(player_id, "pitching")
    games = [g for g in games if g["stat"].get("gamesStarted") == 1]
    today = _date.today().isoformat()
    return build_point_in_time_features(games, today, is_home_today, PITCHER_RAW_FIELDS)
