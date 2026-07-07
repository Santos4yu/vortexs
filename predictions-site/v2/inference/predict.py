"""
Given a live feature dict (from features.py), returns the model's
probability for each stat_type VORTEX V2 supports.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import STAT_TYPES  # noqa: E402
from v2.common.feature_schema import to_vector  # noqa: E402
from v2.inference.model_loader import get_model  # noqa: E402


def predict_all(feature_dict: dict) -> dict:
    """Returns {stat_type: probability} for every stat_type with a loaded
    model. Skips (omits) any stat_type whose model isn't available."""
    vector = [to_vector(feature_dict)]
    out = {}
    for stat_type in STAT_TYPES:
        model = get_model(stat_type)
        if model is None:
            continue
        out[stat_type] = float(model.predict_proba(vector)[0][1])
    return out
