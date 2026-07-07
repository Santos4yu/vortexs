"""
Ordered feature-column lists shared by the training pipeline
(v2/training/build_features.py, via dataset.py) and the deployed inference
path (v2/inference/features.py). Batters and pitchers get independently
shaped feature vectors -- different gamelog group, different raw stats --
rather than one shared schema, so a batter model is never accidentally fed
pitcher-shaped (or vice versa) columns.
"""
from .stat_types import BATTER_RAW_FIELDS, PITCHER_RAW_FIELDS


def _columns_for(raw_fields: dict) -> list:
    stat_names = list(raw_fields)
    return (
        ["games_played_so_far", "is_home", "days_since_last_game"]
        + [f"l{n}_avg_{s}" for n in (5, 10, 20) for s in stat_names]
        + [f"l{n}_rate_{s}_ge1" for n in (5, 10, 20) for s in stat_names]
        + [f"l{n}_games" for n in (5, 10, 20)]
        + [f"season_avg_{s}" for s in stat_names]
    )


BATTER_FEATURE_COLUMNS = _columns_for(BATTER_RAW_FIELDS)
PITCHER_FEATURE_COLUMNS = _columns_for(PITCHER_RAW_FIELDS)


def to_vector(feature_dict: dict, columns: list) -> list:
    """Project a feature dict onto `columns` order, defaulting missing keys to 0.0."""
    return [float(feature_dict.get(c, 0.0)) for c in columns]
