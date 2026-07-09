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


def _num(v, default: float = 0.0) -> float:
    """MLB/Savant stat fields are often dot-strings (".287", "-.--", ".---")
    rather than real numbers -- coerce leniently, defaulting placeholders
    (no data yet) to 0.0 same as the training-side context features do."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_BATTER_CONTEXT_DEFAULTS = {
    "opp_starter_era_prior": 0.0, "opp_starter_fip_prior": 0.0, "opp_bullpen_era_prior": 0.0,
    "opp_team_oaa_prior": 0.0, "own_barrel_pct_prior": 0.0, "own_hard_hit_pct_prior": 0.0,
    "own_xwoba_prior": 0.0, "own_whiff_pct_prior": 0.0,
    "own_avg_vs_opp_hand_prior": 0.0, "own_ops_vs_opp_hand_prior": 0.0, "own_k_pct_vs_opp_hand_prior": 0.0,
    "bvp_avg_through_season": 0.0, "bvp_ops_through_season": 0.0, "bvp_pa_through_season": 0.0,
}


def build_live_context(player_id: int, opponent_team_id: int | None,
                        opponent_pitcher_id: int | None, opponent_pitcher_name: str | None) -> dict:
    """Live counterpart to v2/training/dataset.py's batter context features.
    Column names keep the "_prior"/"through_season" suffixes from the
    training side even though this reads CURRENT-season-to-date stats, not
    last season's full aggregate -- that's an intentional, documented
    train/inference asymmetry (see feature_schema.py's docstring): training
    can only use a prior COMPLETED season without leaking a game's own
    outcome into its own row, whereas live inference naturally wants
    today's most current in-season form, which doesn't have that leakage
    risk (the season is already in progress, not being predicted whole).
    Reuses the exact same stats_mlb functions the Research tab already
    calls live for these signals -- no new integration on this side."""
    out = dict(_BATTER_CONTEXT_DEFAULTS)

    if opponent_pitcher_id and opponent_pitcher_name:
        pitcher = stats_mlb.get_pitcher_metrics(opponent_pitcher_name)
        if not pitcher.get("error"):
            out["opp_starter_era_prior"] = _num(pitcher.get("era"))
            out["opp_starter_fip_prior"] = _num(pitcher.get("fip"))

            hand = pitcher.get("hand") or "R"
            splits = stats_mlb.get_batter_hand_splits(player_id, hand)
            side = splits.get(hand) or {}
            if side:
                out["own_avg_vs_opp_hand_prior"] = _num(side.get("avg"))
                out["own_ops_vs_opp_hand_prior"] = _num(side.get("ops"))
                out["own_k_pct_vs_opp_hand_prior"] = _num(side.get("k_pct"))

        bvp = stats_mlb.get_bvp_history(player_id, opponent_pitcher_id)
        if not bvp.get("error") and bvp.get("ab"):
            out["bvp_avg_through_season"] = _num(bvp.get("avg"))
            out["bvp_ops_through_season"] = _num(bvp.get("ops"))
            out["bvp_pa_through_season"] = float(bvp.get("ab", 0))

    if opponent_team_id:
        bullpen = stats_mlb.get_team_bullpen(opponent_team_id)
        if bullpen.get("era") is not None:
            out["opp_bullpen_era_prior"] = _num(bullpen.get("era"))
        oaa = stats_mlb.get_team_defense_oaa(opponent_team_id)
        if not oaa.get("error"):
            out["opp_team_oaa_prior"] = _num(oaa.get("oaa"))

    # KNOWN LIMITATION: stats_mlb.get_statcast_by_id() sources barrel_pct/
    # hard_hit_pct from a Savant leaderboard whose CSV schema has since
    # changed (confirmed 2026-07 -- those columns no longer come back for
    # ANY year, including live), so those two fields are silently always
    # 0.0 here, same as they already are in the deployed Research tab today.
    # v2/training/fetch_context.py's training-side equivalent uses a
    # different, still-working Savant endpoint for the historical data this
    # model trains on, so training sees real values while live inference
    # doesn't -- a real train/live skew on these two features specifically,
    # out of scope to fix here since it requires patching stats_mlb.py's
    # already-shipped Research integration, not just this file.
    statcast = stats_mlb.get_statcast_by_id(player_id)
    if statcast:
        out["own_barrel_pct_prior"] = _num(statcast.get("barrel_pct"))
        out["own_hard_hit_pct_prior"] = _num(statcast.get("hard_hit_pct"))
        out["own_xwoba_prior"] = _num(statcast.get("xwoba"))
        out["own_chase_pct_prior"] = _num(statcast.get("chase_pct"))
        out["own_whiff_pct_prior"] = _num(statcast.get("whiff_pct"))

    return out


def build_live_pitcher_context(pitcher_id: int, opponent_team_id: int | None) -> dict:
    """Live counterpart to dataset.py's pitcher context features -- own
    arsenal usage + the opposing lineup's current-season batting quality."""
    out = {"own_top_pitch_pct_prior": 0.0, "own_arsenal_size_prior": 0.0,
           "opp_team_ops_prior": 0.0, "opp_team_runs_per_game_prior": 0.0}

    arsenal = stats_mlb.get_pitcher_arsenal(pitcher_id)
    if arsenal:
        pcts = [a.get("pct", 0.0) for a in arsenal]
        out["own_top_pitch_pct_prior"] = round(max(pcts), 1)
        out["own_arsenal_size_prior"] = float(sum(1 for p in pcts if p >= 5.0))

    if opponent_team_id:
        data = stats_mlb._get(f"/teams/{opponent_team_id}/stats", {
            "stats": "season", "group": "hitting", "season": stats_mlb.SEASON,
        }, cache_key=f"team_batting_{opponent_team_id}_{stats_mlb.SEASON}")
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        if splits:
            s = splits[0].get("stat", {})
            games = int(s.get("gamesPlayed", 0) or 0)
            if games > 0:
                out["opp_team_ops_prior"] = _num(s.get("ops"))
                out["opp_team_runs_per_game_prior"] = round(int(s.get("runs", 0) or 0) / games, 2)

    return out
