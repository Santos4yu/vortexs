"""
Point-in-time rolling feature construction for VORTEX V2 training.

Given a player's full-season gamelog (already fetched by fetch_gamelogs.py)
and a target date, computes rolling L5/L10/L20 windows using ONLY games
strictly before that date -- this is the leakage guard. Deliberately
reimplements the rolling-window math rather than calling
predictions-site/backend/stats_mlb.py's get_historical_splits(), which only
ever computes "as of today" and would leak future games into a historical
training example.

Parametrized on `raw_fields` (BATTER_RAW_FIELDS or PITCHER_RAW_FIELDS from
v2/common/stat_types.py) so the same rolling-window logic serves both
batters and pitchers without duplicating it.

Pure / offline -- no network calls.
"""
from datetime import date as _date


def _avg(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def _window_features(prior_games: list, n: int, raw_fields: dict) -> dict:
    window = prior_games[-n:]
    feats = {}
    for stat_name, field in raw_fields.items():
        vals = [float(g["stat"].get(field, 0) or 0) for g in window]
        feats[f"l{n}_avg_{stat_name}"] = round(_avg(vals), 4)
        feats[f"l{n}_rate_{stat_name}_ge1"] = (
            round(sum(1 for v in vals if v >= 1) / len(vals), 4) if vals else 0.0
        )
    feats[f"l{n}_games"] = len(window)
    return feats


def build_point_in_time_features(games: list, target_date: str, target_is_home: bool, raw_fields: dict) -> dict | None:
    """
    `games` must already be sorted ascending by date (fetch_gamelogs.py
    guarantees this). Returns None if there isn't enough prior history
    (fewer than 5 prior games) to build a meaningful feature row -- callers
    should skip these rather than train on near-empty windows.
    """
    prior = [g for g in games if g["date"] < target_date]
    if len(prior) < 5:
        return None

    feats = {
        "games_played_so_far": len(prior),
        "is_home": int(bool(target_is_home)),
    }
    for n in (5, 10, 20):
        feats.update(_window_features(prior, n, raw_fields))

    for stat_name, field in raw_fields.items():
        vals = [float(g["stat"].get(field, 0) or 0) for g in prior]
        feats[f"season_avg_{stat_name}"] = round(_avg(vals), 4)

    try:
        d0 = _date.fromisoformat(prior[-1]["date"])
        d1 = _date.fromisoformat(target_date)
        feats["days_since_last_game"] = (d1 - d0).days
    except ValueError:
        feats["days_since_last_game"] = 1

    return feats
