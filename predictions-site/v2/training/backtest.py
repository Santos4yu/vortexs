"""
Holdout-season backtest for VORTEX V2 models: trains on all seasons except
the most recent one present in the dataset, validates on the held-out
season, and reports calibration + hit-rate-at-confidence-threshold --
directly comparable to V1's own signal_accuracy numbers.

Exit bar (per the VORTEX V2 plan): the model must meet or beat V1's known
tier hit-rate on a genuinely held-out season before Phase 2 (site
integration) starts.

Run directly:
    python backtest.py --csv data/dataset_2023_2024_2025.csv --holdout-season 2025
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
from sklearn.metrics import brier_score_loss, log_loss

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import STAT_TYPES  # noqa: E402
from v2.training.model import make_model  # noqa: E402
from v2.training.train import load_rows, rows_to_xy  # noqa: E402

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


def calibration_table(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> list:
    bins = np.linspace(0, 1, n_bins + 1)
    table = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if mask.sum() == 0:
            continue
        table.append({
            "bucket": f"{lo:.1f}-{hi:.1f}",
            "n": int(mask.sum()),
            "avg_predicted": round(float(y_prob[mask].mean()), 3),
            "actual_hit_rate": round(float(y_true[mask].mean()), 3),
        })
    return table


def hit_rate_at_thresholds(y_true: np.ndarray, y_prob: np.ndarray,
                            thresholds=(0.55, 0.60, 0.65, 0.70)) -> list:
    out = []
    for t in thresholds:
        mask = y_prob >= t
        n = int(mask.sum())
        rate = round(float(y_true[mask].mean()), 4) if n else None
        out.append({"threshold": t, "n": n, "hit_rate": rate})
    return out


def backtest_stat_type(all_rows: list, stat_type: str, holdout_season: str) -> dict:
    train_rows = [r for r in all_rows if r["stat_type"] == stat_type and r["season"] != holdout_season]
    test_rows = [r for r in all_rows if r["stat_type"] == stat_type and r["season"] == holdout_season]
    X_train, y_train = rows_to_xy(train_rows, stat_type)
    X_test, y_test = rows_to_xy(test_rows, stat_type)
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
        "calibration": calibration_table(y_test, y_prob),
        "hit_rate_at_threshold": hit_rate_at_thresholds(y_test, y_prob),
    }


def run(csv_path: Path, holdout_season: str) -> None:
    rows = load_rows(csv_path)
    baseline = v1_baseline()
    print("V1 baseline (signal_accuracy, by stat_type):")
    if baseline:
        for k, v in baseline.items():
            print(f"  {k}: hit_rate={v['hit_rate']} (n={v['total']})")
    else:
        print("  (vortex.db not found or signal_accuracy empty -- no baseline to compare against)")
    print()

    for stat_type in STAT_TYPES:
        result = backtest_stat_type(rows, stat_type, holdout_season)
        print(f"=== {stat_type} ===")
        if "error" in result:
            print(f"  {result['error']}")
            print()
            continue
        print(f"  train n={result['n_train']}  test n={result['n_test']}")
        print(f"  log_loss={result['log_loss']}  brier={result['brier_score']}")
        print(f"  calibration: {result['calibration']}")
        print(f"  hit rate at threshold: {result['hit_rate_at_threshold']}")
        print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--holdout-season", required=True)
    args = parser.parse_args()
    run(Path(args.csv), args.holdout_season)
