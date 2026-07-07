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
from v2.common.stat_types import (  # noqa: E402
    BATTER_STAT_TYPES, PITCHER_STAT_TYPES, BATTER_RAW_FIELDS, PITCHER_RAW_FIELDS,
)
from v2.common.feature_schema import BATTER_FEATURE_COLUMNS, PITCHER_FEATURE_COLUMNS  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
ROW_META_COLUMNS = ["player_id", "player_name", "season", "game_date", "stat_type", "label"]


def _build_rows(gamelogs: dict, season: int, stat_types: tuple, raw_fields: dict, is_pitcher: bool) -> list:
    rows = []
    for player_id, info in gamelogs.items():
        games = info["games"]
        for g in games:
            feats = build_point_in_time_features(games, g["date"], g["is_home"], raw_fields)
            if feats is None:
                continue
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
                rows.append(row)
    return rows


def build_batter_rows_for_season(season: int, limit: int | None = None) -> list:
    gamelogs = fetch_all_gamelogs(season, limit=limit)
    return _build_rows(gamelogs, season, BATTER_STAT_TYPES, BATTER_RAW_FIELDS, is_pitcher=False)


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
    return _build_rows(starts_only, season, PITCHER_STAT_TYPES, PITCHER_RAW_FIELDS, is_pitcher=True)


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
