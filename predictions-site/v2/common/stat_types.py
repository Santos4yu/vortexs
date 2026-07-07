"""
Canonical MLB prop stat types and synthetic standard-line thresholds for
VORTEX V2. Single source of truth shared by the training pipeline and the
deployed inference path, so a threshold or market-key change can't silently
drift between the two.

Home runs deliberately excluded (product decision, 2026-07-06) -- re-add to
BATTER_STAT_TYPES + STANDARD_LINES + MARKET_FOR_STAT if wanted back later.

Batter "walks" and batter "strikeouts" are intentionally NOT included: V1's
own Odds API market map (backend/update_board.py) has no confirmed real
market key for either as a standalone batter prop, only pitcher_strikeouts
exists as a real market -- so even if V2 scored these, there would never be
a real line to check them against on the props board (always a "no market
found" skip). They remain available in V1's Research tab because that
lookup doesn't need a real market to be useful; V2's board does.
"""

# ── Batter props ─────────────────────────────────────────────────────────────

# Raw per-game batting fields, used BOTH as label sources for the "direct"
# stat_types below AND as rolling-window FEATURE inputs for every batter
# model (a player's own history on every one of these, not just the stat
# being predicted, is useful signal for a tree-based model).
BATTER_RAW_FIELDS = {
    "hits": "hits",
    "total_bases": "totalBases",
    "rbis": "rbi",
    "runs_scored": "runs",
}

# stat_types V2 actually predicts for batters. hits_runs_rbis and
# fantasy_score are COMPUTED from several raw fields at once (see
# v2/training/labels.py's compute_batter_actual()), not a single direct
# field, so they need special-cased label logic, not just a dict lookup.
BATTER_COMPUTED_STAT_TYPES = ("hits_runs_rbis", "fantasy_score")
BATTER_STAT_TYPES = tuple(BATTER_RAW_FIELDS) + BATTER_COMPUTED_STAT_TYPES

# ── Pitcher props ────────────────────────────────────────────────────────────

# Raw per-game PITCHING fields (MLB Stats API gameLog group="pitching").
# The API already provides a direct integer "outs" field (confirmed against
# real cached data: "inningsPitched": "5.2" pairs with "outs": 17), so
# pitcher_outs needs no innings-pitched string parsing at all.
PITCHER_RAW_FIELDS = {
    "pitcher_strikeouts": "strikeOuts",
    "pitcher_hits_allowed": "hits",
    "pitcher_earned_runs": "earnedRuns",
    "pitcher_outs": "outs",
}
PITCHER_STAT_TYPES = tuple(PITCHER_RAW_FIELDS)

STAT_TYPES = BATTER_STAT_TYPES + PITCHER_STAT_TYPES

# No historical sportsbook line data exists anywhere in this repo, and buying
# it is its own cost -- see the VORTEX V2 plan's "Known limitations" section.
# Training labels use fixed, standard-sportsbook-style thresholds instead of
# whatever line actually printed that day. The model therefore learns "beats
# a standard line," not "beats today's real line" -- an accepted limitation.
# fantasy_score is the WORST fit for this: its scale varies hugely by a
# player's role (a leadoff hitter and a middle-of-order slugger have wildly
# different typical fantasy scores), so one fixed line is a much rougher
# approximation here than for the count-stat props.
STANDARD_LINES = {
    "hits": 1.5,
    "total_bases": 1.5,
    "rbis": 0.5,
    "runs_scored": 0.5,
    "hits_runs_rbis": 1.5,
    "fantasy_score": 8.5,
    "pitcher_strikeouts": 5.5,
    "pitcher_hits_allowed": 5.5,
    "pitcher_earned_runs": 2.5,
    "pitcher_outs": 16.5,
}

# stat_type -> The Odds API market key. Matches backend/update_board.py's own
# MARKET_TO_PROP_TYPE exactly (same real markets V1 already fetches), so V2
# is checking against markets confirmed to actually exist.
MARKET_FOR_STAT = {
    "hits": "batter_hits",
    "total_bases": "batter_total_bases",
    "rbis": "batter_rbis",
    "runs_scored": "batter_runs_scored",
    "hits_runs_rbis": "batter_hits_runs_rbis",
    "fantasy_score": "batter_fantasy_score",
    "pitcher_strikeouts": "pitcher_strikeouts",
    "pitcher_hits_allowed": "pitcher_hits_allowed",
    "pitcher_earned_runs": "pitcher_earned_runs",
    "pitcher_outs": "pitcher_outs",
}

STAT_LABELS = {
    "hits": "Hits",
    "total_bases": "Total Bases",
    "rbis": "RBIs",
    "runs_scored": "Runs Scored",
    "hits_runs_rbis": "Hits+Runs+RBIs",
    "fantasy_score": "Fantasy Score (PP)",
    "pitcher_strikeouts": "Strikeouts (P)",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_earned_runs": "Earned Runs",
    "pitcher_outs": "Outs",
}

DEFAULT_SIDE = "over"
