"""
Loads the joblib model artifacts trained by v2/training/train.py.
Module-level singleton cache -- in a Vercel function this means "load once
per cold start," same pattern stats_mlb.py uses for its active-players cache.
"""
import json
from pathlib import Path

import joblib

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"

_MODELS: dict = {}
_MANIFEST: dict | None = None


def get_manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        manifest_path = MODELS_DIR / "manifest.json"
        _MANIFEST = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"stat_types": {}}
    return _MANIFEST


def get_model(stat_type: str):
    if stat_type not in _MODELS:
        info = get_manifest().get("stat_types", {}).get(stat_type)
        if not info:
            return None
        _MODELS[stat_type] = joblib.load(MODELS_DIR / info["model_file"])
    return _MODELS[stat_type]
