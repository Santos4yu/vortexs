"""
Canonical MLB prop stat types and synthetic standard-line thresholds for
VORTEX V2. Single source of truth shared by the training pipeline and the
(future) deployed inference path, so a threshold change can't silently drift
between the two.
"""

# Stat types VORTEX V2 currently trains on -- the highest-volume batter props
# (see VORTEX V2 plan, Phase 1). All three are present directly in a batter's
# MLB Stats API gameLog `stat` block, so no extra boxscore fetch is needed.
STAT_TYPES = ("hits", "total_bases", "home_runs")

# stat_type -> field name in the gameLog `stat` dict (see
# predictions-site/backend/stats_mlb.py's PROP_STAT_MAP for the V1 equivalent
# mapping used by the live site/bot).
GAMELOG_FIELD = {
    "hits": "hits",
    "total_bases": "totalBases",
    "home_runs": "homeRuns",
}

# No historical sportsbook line data exists anywhere in this repo, and buying
# it is its own cost -- see the VORTEX V2 plan's "Known limitations" section.
# Training labels use fixed, standard-sportsbook-style thresholds instead of
# whatever line actually printed that day. The model therefore learns "beats
# a standard line," not "beats today's real line" -- an accepted limitation.
# Phase 3's live board scores the model against the real posted line at
# inference time; this mismatch only affects training-time labels.
STANDARD_LINES = {
    "hits": 1.5,
    "total_bases": 1.5,
    "home_runs": 0.5,
}

DEFAULT_SIDE = "over"
