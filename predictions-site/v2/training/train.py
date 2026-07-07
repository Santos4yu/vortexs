"""
Trains one model per stat_type (see model.py) on a dataset CSV built by
dataset.py. Writes joblib artifacts + manifest.json to v2/models/ -- these
are the only training-pipeline outputs that ever get committed/deployed;
everything else under v2/training/ stays local.

Run directly:
    python train.py --csv data/dataset_2023_2024_2025.csv
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import STAT_TYPES  # noqa: E402
from v2.common.feature_schema import FEATURE_COLUMNS  # noqa: E402
from v2.training.model import make_model  # noqa: E402

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"


def load_rows(csv_path: Path) -> list:
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def rows_to_xy(rows: list, stat_type: str):
    filtered = [r for r in rows if r["stat_type"] == stat_type]
    X = np.array([[float(r[c]) for c in FEATURE_COLUMNS] for r in filtered], dtype=float)
    y = np.array([int(r["label"]) for r in filtered], dtype=int)
    return X, y


def train_one(rows: list, stat_type: str) -> dict:
    X, y = rows_to_xy(rows, stat_type)
    if len(y) < 50:
        print(f"  [{stat_type}] only {len(y)} rows -- skipping (need >= 50)")
        return {}
    model = make_model()
    model.fit(X, y)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODELS_DIR / f"{stat_type}_v1.joblib"
    joblib.dump(model, model_path)
    return {
        "stat_type": stat_type,
        "model_file": model_path.name,
        "n_rows": len(y),
        "base_rate": round(float(y.mean()), 4),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_columns": list(FEATURE_COLUMNS),
    }


def train_all(csv_path: Path) -> None:
    rows = load_rows(csv_path)
    manifest = {"source_csv": csv_path.name, "stat_types": {}}
    for stat_type in STAT_TYPES:
        print(f"Training {stat_type}...")
        info = train_one(rows, stat_type)
        if info:
            manifest["stat_types"][stat_type] = info
    manifest_path = MODELS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote manifest to {manifest_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    args = parser.parse_args()
    train_all(Path(args.csv))
