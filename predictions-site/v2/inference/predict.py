"""
Given a live feature dict (from features.py), returns the model's
probability for each stat_type VORTEX V2 supports.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import BATTER_STAT_TYPES, PITCHER_STAT_TYPES  # noqa: E402
from v2.common.feature_schema import BATTER_FEATURE_COLUMNS, PITCHER_FEATURE_COLUMNS, to_vector  # noqa: E402
from v2.inference.model_loader import get_model, get_manifest  # noqa: E402


def _model_columns(stat_type: str, fallback: list) -> list:
    """The feature columns THIS model artifact was trained on, from the
    manifest. The live schema (feature_schema.py) can grow ahead of the
    deployed .joblib files -- feeding an old model the current column list
    crashes with "X has 48 features, but ... is expecting 34". Each model
    gets exactly its own training-time columns; newly added features only
    take effect once the model is retrained (which rewrites the manifest)."""
    info = get_manifest().get("stat_types", {}).get(stat_type) or {}
    return info.get("feature_columns") or fallback


def predict_all(feature_dict: dict) -> dict:
    """Batter version. Returns {stat_type: probability} for every batter
    stat_type with a loaded model. Skips (omits) any stat_type whose model
    isn't available."""
    out = {}
    for stat_type in BATTER_STAT_TYPES:
        model = get_model(stat_type)
        if model is None:
            continue
        vector = [to_vector(feature_dict, _model_columns(stat_type, BATTER_FEATURE_COLUMNS))]
        out[stat_type] = float(model.predict_proba(vector)[0][1])
    return out


def predict_all_pitcher(feature_dict: dict) -> dict:
    """Pitcher version -- same idea, pitcher stat_types + feature schema."""
    out = {}
    for stat_type in PITCHER_STAT_TYPES:
        model = get_model(stat_type)
        if model is None:
            continue
        vector = [to_vector(feature_dict, _model_columns(stat_type, PITCHER_FEATURE_COLUMNS))]
        out[stat_type] = float(model.predict_proba(vector)[0][1])
    return out
