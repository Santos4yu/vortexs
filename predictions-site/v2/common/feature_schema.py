"""
Ordered feature-column list shared by the training pipeline
(v2/training/build_features.py, via dataset.py) and the future deployed
inference path (v2/inference/features.py). Importing from one place is what
keeps the two from silently drifting apart as features get added.
"""
from .stat_types import STAT_TYPES

FEATURE_COLUMNS = (
    ["games_played_so_far", "is_home", "days_since_last_game"]
    + [f"l{n}_avg_{s}" for n in (5, 10, 20) for s in STAT_TYPES]
    + [f"l{n}_rate_{s}_ge1" for n in (5, 10, 20) for s in STAT_TYPES]
    + [f"l{n}_games" for n in (5, 10, 20)]
    + [f"season_avg_{s}" for s in STAT_TYPES]
)


def to_vector(feature_dict: dict) -> list:
    """Project a feature dict onto FEATURE_COLUMNS order, defaulting missing keys to 0.0."""
    return [float(feature_dict.get(c, 0.0)) for c in FEATURE_COLUMNS]
