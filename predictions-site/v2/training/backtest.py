"""
Holdout-season backtest for VORTEX V2 models: trains on all seasons except
the most recent one present in the dataset, validates on the held-out
season, and reports calibration + precision-at-top-K (the metric that
actually matters here -- see the VORTEX V2 conversation history for why raw
hit-rate-at-absolute-confidence-threshold doesn't work for fixed synthetic
lines whose true probability rarely clears 50%).

Run directly:
    python backtest.py --csv data/dataset_batters_2023_2024_2025.csv --kind batter --holdout-season 2025
    python backtest.py --csv data/dataset_pitchers_2023_2024_2025.csv --kind pitcher --holdout-season 2025
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
from sklearn.metrics import brier_score_loss, log_loss

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.training.model import make_model  # noqa: E402
from v2.training.train import KIND_CONFIG, load_rows, rows_to_xy  # noqa: E402

VORTEX_DB = Path(__file__).resolve().parents[3] / "vortex.db"


def v1_baseline() -> dict:
    """Read-only, one-shot: V1's own known hit rates by stat_type, from
    vortex.db's signal_accuracy table (bot-side; V2 never depends on this
    at runtime -- this is purely for the backtest comparison report)."""
    if not VORTEX_DB.exists():
        return {}
    conn = sqlite3.connect(VORTEX_DB)
    cur = conn.execute(
        "SELECT value, hit_rate, total FROM signal_accuracy WHERE dimension='stat_type'"
    )
    return {row[0]: {"hit_rate": row[1], "total": row[2]} for row in cur.fetchall()}


def topk_report(y_true: np.ndarray, y_prob: np.ndarray, fracs=(0.05, 0.10, 0.20)) -> list:
    order = np.argsort(-y_prob)
    n = len(y_prob)
    base_rate = float(y_true.mean()) if n else 0.0
    out = [{"base_rate": round(base_rate, 4), "n": n}]
    for frac in fracs:
        k = max(1, int(n * frac))
        idx = order[:k]
        out.append({"top_pct": int(frac * 100), "n": k, "hit_rate": round(float(y_true[idx].mean()), 4)})
    return out


def backtest_stat_type(all_rows: list, stat_type: str, feature_columns: list, holdout_season: str) -> dict:
    train_rows = [r for r in all_rows if r["stat_type"] == stat_type and r["season"] != holdout_season]
    test_rows = [r for r in all_rows if r["stat_type"] == stat_type and r["season"] == holdout_season]
    X_train, y_train = rows_to_xy(train_rows, stat_type, feature_columns)
    X_test, y_test = rows_to_xy(test_rows, stat_type, feature_columns)
    if len(y_train) < 50 or len(y_test) < 20:
        return {"stat_type": stat_type, "error": f"insufficient rows (train={len(y_train)}, test={len(y_test)})"}

    model = make_model()
    model.fit(X_train, y_train)
    y_prob = model.predict_proba(X_test)[:, 1]

    return {
        "stat_type": stat_type,
        "n_train": len(y_train),
        "n_test": len(y_test),
        "log_loss": round(float(log_loss(y_test, y_prob)), 4),
        "brier_score": round(float(brier_score_loss(y_test, y_prob)), 4),
        "topk": topk_report(y_test, y_prob),
    }


def run(csv_path: Path, kind: str, holdout_season: str) -> None:
    stat_types, feature_columns = KIND_CONFIG[kind]
    rows = load_rows(csv_path)
    baseline = v1_baseline()
    print("V1 baseline (signal_accuracy, by stat_type):")
    if baseline:
        for k, v in baseline.items():
            print(f"  {k}: hit_rate={v['hit_rate']} (n={v['total']})")
    else:
        print("  (vortex.db not found or signal_accuracy empty -- no baseline to compare against)")
    print()

    for stat_type in stat_types:
        result = backtest_stat_type(rows, stat_type, feature_columns, holdout_season)
        print(f"=== {stat_type} ===")
        if "error" in result:
            print(f"  {result['error']}")
            print()
            continue
        print(f"  train n={result['n_train']}  test n={result['n_test']}")
        print(f"  log_loss={result['log_loss']}  brier={result['brier_score']}")
        print(f"  top-K: {result['topk']}")
        print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--kind", required=True, choices=["batter", "pitcher"])
    parser.add_argument("--holdout-season", required=True)
    args = parser.parse_args()
    run(Path(args.csv), args.kind, args.holdout_season)
