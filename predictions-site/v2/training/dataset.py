"""
Assembles the VORTEX V2 training dataset: for every batter/season fetched by
fetch_gamelogs.py, walks each game chronologically and emits one row per
(player, game, stat_type) with point-in-time features + a hit/miss label.
Pushes are excluded (a binary classifier has no third class for them).

Offline only. Run directly:
    python dataset.py --seasons 2023 2024 2025 --limit 150

Output is a CSV under v2/training/data/ that train.py and backtest.py both
consume without re-fetching anything.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.training.fetch_gamelogs import fetch_all_gamelogs  # noqa: E402
from v2.training.build_features import build_point_in_time_features  # noqa: E402
from v2.training.labels import label_for_game  # noqa: E402
from v2.common.stat_types import STAT_TYPES  # noqa: E402
from v2.common.feature_schema import FEATURE_COLUMNS  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
ROW_META_COLUMNS = ["player_id", "player_name", "season", "game_date", "stat_type", "label"]


def build_rows_for_season(season: int, limit: int | None = None) -> list:
    gamelogs = fetch_all_gamelogs(season, limit=limit)
    rows = []
    for player_id, info in gamelogs.items():
        games = info["games"]
        for g in games:
            feats = build_point_in_time_features(games, g["date"], g["is_home"])
            if feats is None:
                continue
            for stat_type in STAT_TYPES:
                label = label_for_game(g, stat_type)
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


def write_csv(rows: list, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ROW_META_COLUMNS + FEATURE_COLUMNS
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_and_save(seasons: list, limit: int | None = None) -> Path:
    all_rows = []
    for season in seasons:
        print(f"Building rows for {season}...")
        rows = build_rows_for_season(season, limit=limit)
        print(f"  {len(rows)} rows")
        all_rows.extend(rows)
    out_path = DATA_DIR / f"dataset_{'_'.join(map(str, seasons))}.csv"
    write_csv(all_rows, out_path)
    print(f"Wrote {len(all_rows)} rows to {out_path}")
    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", type=int, nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=None,
                         help="cap the number of batters fetched per season (for a quick test run)")
    args = parser.parse_args()
    build_and_save(args.seasons, limit=args.limit)
