"""
Assembles the VORTEX V2 training dataset: for every player/season fetched by
fetch_gamelogs.py, walks each game chronologically and emits one row per
(player, game, stat_type) with point-in-time features + a hit/miss label.
Pushes are excluded (a binary classifier has no third class for them).

Batter and pitcher rows are built separately (different gamelog group,
different feature schema) and written to separate CSVs, since they train
separate models with separate feature columns.

Offline only. Run directly:
    python dataset.py --seasons 2023 2024 2025 --limit 150
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.training.fetch_gamelogs import (  # noqa: E402
    fetch_all_gamelogs, fetch_all_pitcher_gamelogs,
)
from v2.training.build_features import build_point_in_time_features  # noqa: E402
from v2.training.labels import label_for_game  # noqa: E402
from v2.training.resolve_starters import resolve_starters_for_games  # noqa: E402
from v2.training import fetch_context as ctx  # noqa: E402
from v2.common.stat_types import (  # noqa: E402
    BATTER_STAT_TYPES, PITCHER_STAT_TYPES, BATTER_RAW_FIELDS, PITCHER_RAW_FIELDS,
)
from v2.common.feature_schema import BATTER_FEATURE_COLUMNS, PITCHER_FEATURE_COLUMNS  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
ROW_META_COLUMNS = ["player_id", "player_name", "season", "game_date", "stat_type", "label"]


def _build_rows(gamelogs: dict, season: int, stat_types: tuple, raw_fields: dict,
                 is_pitcher: bool, context_fn) -> list:
    rows = []
    for player_id, info in gamelogs.items():
        games = info["games"]
        for g in games:
            feats = build_point_in_time_features(games, g["date"], g["is_home"], raw_fields)
            if feats is None:
                continue
            context = context_fn(player_id, g, season)
            for stat_type in stat_types:
                label = label_for_game(g, stat_type, is_pitcher=is_pitcher)
                if label == "push":
                    continue
                row = {
                    "player_id": player_id,
                    "player_name": info["fullName"],
                    "season": season,
                    "game_date": g["date"],
                    "stat_type": stat_type,
                    "label": 1 if label == "hit" else 0,
                }
                row.update(feats)
                row.update(context)
                rows.append(row)
    return rows


def _batter_context_builder(gamelogs: dict, season: int):
    """Returns a context_fn(player_id, game, season) -> dict for batter rows.
    Preloads the whole-league, prior-season OAA/Statcast tables once (they're
    single leaderboard calls each) and resolves every distinct game_pk's
    starters once up front, so the per-row work below is all disk-cache hits
    or a small number of new per-entity network calls -- never duplicated
    across the many rows that share the same opponent/pitcher/batter."""
    prior_season = season - 1
    game_pks = {g["game_pk"] for info in gamelogs.values() for g in info["games"] if g.get("game_pk")}
    print(f"  resolving opposing starters for {len(game_pks)} distinct games...")
    starters_by_game = resolve_starters_for_games(game_pks)
    league_oaa = ctx.fetch_league_oaa(prior_season)
    league_statcast = ctx.fetch_league_batter_statcast(prior_season)

    def context_fn(player_id: int, g: dict, season: int) -> dict:
        out = {c: 0.0 for c in [
            "opp_starter_era_prior", "opp_starter_fip_prior", "opp_bullpen_era_prior",
            "opp_team_oaa_prior", "own_barrel_pct_prior", "own_hard_hit_pct_prior",
            "own_xwoba_prior", "own_whiff_pct_prior",
            "own_avg_vs_opp_hand_prior", "own_ops_vs_opp_hand_prior", "own_k_pct_vs_opp_hand_prior",
            "bvp_avg_through_season", "bvp_ops_through_season", "bvp_pa_through_season",
        ]}
        opponent_id = g.get("opponent_id")
        game_pk = g.get("game_pk")

        opp_starter_id = (starters_by_game.get(game_pk) or {}).get(opponent_id) if opponent_id and game_pk else None
        if opp_starter_id:
            q = ctx.fetch_pitcher_season_quality(opp_starter_id, prior_season)
            if q.get("era") is not None:
                out["opp_starter_era_prior"] = q["era"]
            if q.get("fip") is not None:
                out["opp_starter_fip_prior"] = q["fip"]

            hand = ctx.fetch_pitcher_hand(opp_starter_id)
            splits = ctx.fetch_batter_hand_splits_season(player_id, prior_season)
            side = splits.get(hand)
            if side:
                out["own_avg_vs_opp_hand_prior"] = side.get("avg", 0.0)
                out["own_ops_vs_opp_hand_prior"] = side.get("ops", 0.0)
                out["own_k_pct_vs_opp_hand_prior"] = side.get("k_pct", 0.0)

            bvp = ctx.fetch_bvp_through_season(player_id, opp_starter_id, season)
            if bvp:
                out["bvp_avg_through_season"] = bvp.get("avg", 0.0)
                out["bvp_ops_through_season"] = bvp.get("ops", 0.0)
                out["bvp_pa_through_season"] = bvp.get("pa", 0.0)

        if opponent_id:
            bp = ctx.fetch_team_bullpen_quality(opponent_id, prior_season)
            if bp.get("era") is not None:
                out["opp_bullpen_era_prior"] = bp["era"]
            oaa = league_oaa.get(str(opponent_id))
            if oaa is not None:
                out["opp_team_oaa_prior"] = oaa

        sc = league_statcast.get(str(player_id))
        if sc:
            out["own_barrel_pct_prior"] = sc.get("barrel_pct", 0.0)
            out["own_hard_hit_pct_prior"] = sc.get("hard_hit_pct", 0.0)
            out["own_xwoba_prior"] = sc.get("xwoba", 0.0)
            out["own_whiff_pct_prior"] = sc.get("whiff_pct", 0.0)

        return out

    return context_fn


def _pitcher_context_builder(season: int):
    prior_season = season - 1

    def context_fn(player_id: int, g: dict, season: int) -> dict:
        out = {"own_top_pitch_pct_prior": 0.0, "own_arsenal_size_prior": 0.0,
               "opp_team_ops_prior": 0.0, "opp_team_runs_per_game_prior": 0.0}
        arsenal = ctx.fetch_pitcher_arsenal_summary(player_id, prior_season)
        if arsenal:
            out["own_top_pitch_pct_prior"] = arsenal.get("top_pitch_pct", 0.0)
            out["own_arsenal_size_prior"] = arsenal.get("arsenal_size", 0.0)
        opponent_id = g.get("opponent_id")
        if opponent_id:
            bat = ctx.fetch_team_season_batting(opponent_id, prior_season)
            if bat:
                out["opp_team_ops_prior"] = bat.get("ops", 0.0)
                out["opp_team_runs_per_game_prior"] = bat.get("runs_per_game", 0.0)
        return out

    return context_fn


def build_batter_rows_for_season(season: int, limit: int | None = None) -> list:
    gamelogs = fetch_all_gamelogs(season, limit=limit)
    context_fn = _batter_context_builder(gamelogs, season)
    return _build_rows(gamelogs, season, BATTER_STAT_TYPES, BATTER_RAW_FIELDS, is_pitcher=False,
                        context_fn=context_fn)


def build_pitcher_rows_for_season(season: int, limit: int | None = None) -> list:
    gamelogs = fetch_all_pitcher_gamelogs(season, limit=limit)
    # Real sportsbook pitcher props (strikeouts, outs, hits allowed, earned
    # runs) are posted for STARTS specifically -- a reliever's 1-inning
    # relief appearance isn't what "5.5 strikeouts" means. Filtering to
    # gamesStarted==1 keeps both the labeled rows AND each pitcher's rolling
    # history limited to their own starts, not relief innings that would
    # otherwise massively understate a starter's typical performance.
    starts_only = {
        pid: {"fullName": info["fullName"],
              "games": [g for g in info["games"] if g["stat"].get("gamesStarted") == 1]}
        for pid, info in gamelogs.items()
    }
    starts_only = {pid: info for pid, info in starts_only.items() if info["games"]}
    context_fn = _pitcher_context_builder(season)
    return _build_rows(starts_only, season, PITCHER_STAT_TYPES, PITCHER_RAW_FIELDS, is_pitcher=True,
                        context_fn=context_fn)


def write_csv(rows: list, out_path: Path, feature_columns: list) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ROW_META_COLUMNS + feature_columns
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_and_save(seasons: list, limit: int | None = None) -> tuple:
    all_batter_rows, all_pitcher_rows = [], []
    for season in seasons:
        print(f"Building batter rows for {season}...")
        rows = build_batter_rows_for_season(season, limit=limit)
        print(f"  {len(rows)} rows")
        all_batter_rows.extend(rows)

        print(f"Building pitcher rows for {season}...")
        rows = build_pitcher_rows_for_season(season, limit=limit)
        print(f"  {len(rows)} rows")
        all_pitcher_rows.extend(rows)

    season_tag = "_".join(map(str, seasons))
    batter_path = DATA_DIR / f"dataset_batters_{season_tag}.csv"
    pitcher_path = DATA_DIR / f"dataset_pitchers_{season_tag}.csv"
    write_csv(all_batter_rows, batter_path, BATTER_FEATURE_COLUMNS)
    write_csv(all_pitcher_rows, pitcher_path, PITCHER_FEATURE_COLUMNS)
    print(f"Wrote {len(all_batter_rows)} batter rows to {batter_path}")
    print(f"Wrote {len(all_pitcher_rows)} pitcher rows to {pitcher_path}")
    return batter_path, pitcher_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", type=int, nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=None,
                         help="cap the number of players fetched per season (for a quick test run)")
    args = parser.parse_args()
    build_and_save(args.seasons, limit=args.limit)
