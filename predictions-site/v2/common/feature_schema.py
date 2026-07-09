"""
Ordered feature-column lists shared by the training pipeline
(v2/training/build_features.py, via dataset.py) and the deployed inference
path (v2/inference/features.py). Batters and pitchers get independently
shaped feature vectors -- different gamelog group, different raw stats --
rather than one shared schema, so a batter model is never accidentally fed
pitcher-shaped (or vice versa) columns.

CONTEXT_COLUMNS (added 2026-07) port Research's matchup/skill signal
categories (backend/analyze.py's _v2_matchup/_v2_skill) into the trained
models -- see v2/training/fetch_context.py's docstring for the full
provenance + leakage-rule writeup. Every one of these is the PRIOR season's
aggregate (never the target season's own, in-progress-or-full aggregate) to
avoid leaking a game's own outcome into its own feature row. Weather, lineup
batting-order slot, and umpire tendency are deliberately NOT included here
(no reconstructable historical source for two of them; explicitly deferred
as a separate follow-up project for the third -- see the VORTEX V2 plan).
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


# Opposing-matchup + own-skill context, prior-season aggregates.
BATTER_CONTEXT_COLUMNS = [
    "opp_starter_era_prior", "opp_starter_fip_prior",   # who's on the mound (_v2_matchup.pitcher)
    "opp_bullpen_era_prior",                            # _v2_matchup.opp_bullpen
    "opp_team_oaa_prior",                                # _v2_matchup.oaa
    "own_barrel_pct_prior", "own_hard_hit_pct_prior",   # _v2_skill.statcast
    "own_xwoba_prior", "own_whiff_pct_prior",
    "own_avg_vs_opp_hand_prior", "own_ops_vs_opp_hand_prior",  # _v2_skill.vs_hand_splits,
    "own_k_pct_vs_opp_hand_prior",                             # conditioned on today's actual opposing hand
    "bvp_avg_through_season", "bvp_ops_through_season", "bvp_pa_through_season",  # _v2_skill.bvp
]

# Own arsenal + opposing lineup quality (pitcher rows face a LINEUP's batting
# quality, not a defense -- OAA doesn't apply here the way it does for batters).
PITCHER_CONTEXT_COLUMNS = [
    "own_top_pitch_pct_prior", "own_arsenal_size_prior",   # _v2_skill.arsenal
    "opp_team_ops_prior", "opp_team_runs_per_game_prior",  # _v2_matchup-equivalent for facing a lineup
]

BATTER_FEATURE_COLUMNS = _columns_for(BATTER_RAW_FIELDS) + BATTER_CONTEXT_COLUMNS
PITCHER_FEATURE_COLUMNS = _columns_for(PITCHER_RAW_FIELDS) + PITCHER_CONTEXT_COLUMNS


def to_vector(feature_dict: dict, columns: list) -> list:
    """Project a feature dict onto `columns` order, defaulting missing keys to 0.0."""
    return [float(feature_dict.get(c, 0.0)) for c in columns]
