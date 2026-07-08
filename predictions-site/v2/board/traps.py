"""
Bait-prop (trap) detection for the VORTEX V2 props board.

The thing hit-rate research apps sell hardest -- "over in 5 straight games!"
-- is exactly what a sportsbook wants the public to see, because a streak
carries zero information about TONIGHT's matchup. This module finds today's
players whose recent streak (>= BAIT_MIN_OVER of their last 5 games over the
standard line) makes them look automatic, but whose matchup context says the
streak is bait:

    PLATOON TRAP  streak is real, but he's bad against tonight's starter's
                  hand (season split, min 40 PA so a cold 10-PA sample can't
                  fire it)
    BVP TRAP      meaningful career history vs tonight's starter and it's
                  ugly (min 8 AB)
    ACE TRAP      tonight's starter is elite (ERA <= 3.00 or FIP <= 3.20)
    WHIFF TRAP    hit-dependent stat + high K% vs tonight's starter's hand
    LINEUP TRAP   (pitchers) K/outs streak, but tonight's opposing lineup is
                  a top offense (OPS >= .760 or >= 5.0 runs/game)

A streak alone is never a trap -- at least one matchup hook must fire. Every
lookup here is a disk-cache hit: the same gamelog/splits/BvP/pitcher-metrics
calls were already made moments earlier by inference/features.py's live
feature build for the same player, so trap detection costs no extra network
and ZERO Odds API credits.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import stats_mlb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import (  # noqa: E402
    BATTER_STAT_TYPES, STANDARD_LINES, STAT_LABELS,
)
from v2.inference.features import fetch_current_season_gamelog  # noqa: E402
from v2.training.labels import compute_batter_actual, compute_pitcher_actual  # noqa: E402

BAIT_MIN_OVER = 4          # >= this many of the last 5 games over the line
STREAK_WINDOW = 5

# Hook thresholds. Sample-size guards matter more than the cutoffs
# themselves: a hook that fires on noise turns the Bait tab into noise.
PLATOON_MIN_PA = 40
PLATOON_MAX_AVG = 0.220
PLATOON_MAX_OPS = 0.640
WHIFF_MIN_K_PCT = 27.0     # k_pct is 0-100 scale (see stats_mlb.get_batter_hand_splits)
BVP_MIN_AB = 8
BVP_MAX_AVG = 0.150
ACE_MAX_ERA = 3.00
ACE_MAX_FIP = 3.20
LINEUP_MIN_OPS = 0.760
LINEUP_MIN_RPG = 5.0

# Stats where reaching the line requires base hits, so a high K% vs
# tonight's hand directly attacks the streak.
_HIT_DEPENDENT = {"hits", "total_bases", "hits_runs_rbis", "fantasy_score"}

# Ks and outs are the pitcher overs the public chases off a hot stretch.
# hits_allowed/earned_runs streaks bait the UNDER, whose hostile-matchup
# logic is inverted -- out of scope for this first version.
_PITCHER_TRAP_STATS = ("pitcher_strikeouts", "pitcher_outs")

_HOOK_PRIORITY = ("PLATOON TRAP", "BVP TRAP", "ACE TRAP", "WHIFF TRAP", "LINEUP TRAP")

MAX_PER_PLAYER = 2


def _num(v, default: float = 0.0) -> float:
    """Same lenient coercion features.py uses for MLB's dot-string stats."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_avg(v: float) -> str:
    return f"{v:.3f}".lstrip("0")


def _streak(games: list, stat_type: str, is_pitcher: bool) -> tuple[int, int]:
    """(games over the standard line, games looked at) across the last
    STREAK_WINDOW entries. Lines are all x.5, so > line has no push case."""
    recent = games[-STREAK_WINDOW:]
    line = STANDARD_LINES[stat_type]
    compute = compute_pitcher_actual if is_pitcher else compute_batter_actual
    over = sum(1 for g in recent if compute(g["stat"], stat_type) > line)
    return over, len(recent)


def _card(job: dict, stat_type: str, over: int, window: int,
          hooks: list, trap_types: list, model_prob: float | None) -> dict:
    naive = over / window
    severity = 0.5 * len(hooks) + (max(0.0, naive - model_prob) if model_prob is not None else 0.0)
    label = next((t for t in _HOOK_PRIORITY if t in trap_types), trap_types[0])
    return {
        "game_pk": job["game_pk"],
        "home_team_name": job["home_team_name"],
        "away_team_name": job["away_team_name"],
        "player_id": job["player_id"],
        "player_name": job["player_name"],
        "stat_type": stat_type,
        "stat_label": STAT_LABELS.get(stat_type, stat_type),
        "line": STANDARD_LINES[stat_type],
        "streak_over": over,
        "streak_games": window,
        "bait": f"Over {STANDARD_LINES[stat_type]} {STAT_LABELS.get(stat_type, stat_type)} "
                f"in {over} of his last {window}",
        "hooks": hooks,
        "trap_types": trap_types,
        "trap_label": label,
        "naive_prob": round(naive, 4),
        "model_prob": round(model_prob, 4) if model_prob is not None else None,
        "severity": round(severity, 4),
    }


def detect_batter_traps(job: dict, probs: dict) -> list:
    """Trap cards for one batter job from build_board.score_todays_slate.
    `probs` is predict_all()'s {stat_type: model_prob} for this batter."""
    games = fetch_current_season_gamelog(job["player_id"], "hitting")
    if len(games) < STREAK_WINDOW:
        return []

    # Streaks first (pure gamelog math) -- only pay the (cached) matchup
    # lookups if at least one stat actually has a bait streak.
    streaks = {}
    for stat_type in BATTER_STAT_TYPES:
        over, window = _streak(games, stat_type, is_pitcher=False)
        if window >= STREAK_WINDOW and over >= BAIT_MIN_OVER:
            streaks[stat_type] = (over, window)
    if not streaks:
        return []

    pitcher_name = job.get("opp_pitcher_name")
    pitcher_id = job.get("opp_pitcher_id")
    if not pitcher_name:
        return []  # every batter hook is starter-relative; no starter, no verdict

    pitcher = stats_mlb.get_pitcher_metrics(pitcher_name)
    if pitcher.get("error"):
        return []
    hand = (pitcher.get("hand") or "R")[:1].upper()
    hand_word = "lefty" if hand == "L" else "righty"
    hand_plural = "lefties" if hand == "L" else "righties"
    era = _num(pitcher.get("era"))
    fip = _num(pitcher.get("fip"))

    side = (stats_mlb.get_batter_hand_splits(job["player_id"], hand) or {}).get(hand) or {}
    pa_vs_hand = int(side.get("pa") or 0)
    avg_vs_hand = _num(side.get("avg"))
    ops_vs_hand = _num(side.get("ops"))
    k_pct_vs_hand = _num(side.get("k_pct"))

    bvp = stats_mlb.get_bvp_history(job["player_id"], pitcher_id) if pitcher_id else {}
    bvp_ab = int(bvp.get("ab") or 0) if not bvp.get("error") else 0
    bvp_hits = int(bvp.get("hits") or 0)
    bvp_avg = _num(bvp.get("avg"))

    # Matchup hooks that don't depend on which stat is streaking.
    shared_hooks: list[tuple[str, str]] = []
    if pa_vs_hand >= PLATOON_MIN_PA and 0 < avg_vs_hand <= PLATOON_MAX_AVG:
        shared_hooks.append(("PLATOON TRAP",
                             f"Hitting just {_fmt_avg(avg_vs_hand)} vs {hand_plural} this season "
                             f"({pa_vs_hand} PA) — tonight's starter {pitcher_name} is a {hand_word}"))
    elif pa_vs_hand >= PLATOON_MIN_PA and 0 < ops_vs_hand <= PLATOON_MAX_OPS:
        shared_hooks.append(("PLATOON TRAP",
                             f"{_fmt_avg(ops_vs_hand)} OPS vs {hand_plural} this season "
                             f"({pa_vs_hand} PA) — tonight's starter {pitcher_name} is a {hand_word}"))
    if bvp_ab >= BVP_MIN_AB and bvp_avg <= BVP_MAX_AVG:
        shared_hooks.append(("BVP TRAP",
                             f"{bvp_hits}-for-{bvp_ab} ({_fmt_avg(bvp_avg)}) lifetime vs {pitcher_name}"))
    if 0 < era <= ACE_MAX_ERA:
        shared_hooks.append(("ACE TRAP",
                             f"{pitcher_name} is no cupcake: {era:.2f} ERA ({fip:.2f} FIP)"))
    elif 0 < fip <= ACE_MAX_FIP:
        shared_hooks.append(("ACE TRAP",
                             f"{pitcher_name} is better than his {era:.2f} ERA looks — "
                             f"{fip:.2f} FIP, ace stuff under the hood"))

    whiff_hook = None
    if pa_vs_hand >= PLATOON_MIN_PA and k_pct_vs_hand >= WHIFF_MIN_K_PCT:
        whiff_hook = ("WHIFF TRAP",
                      f"Strikes out {k_pct_vs_hand:.1f}% of the time vs {hand_plural}")

    cards = []
    for stat_type, (over, window) in streaks.items():
        hooks = list(shared_hooks)
        if whiff_hook and stat_type in _HIT_DEPENDENT:
            hooks.append(whiff_hook)
        if not hooks:
            continue  # a streak with no hostile context is just a streak
        cards.append(_card(job, stat_type, over, window,
                           [h[1] for h in hooks], [h[0] for h in hooks],
                           probs.get(stat_type)))

    cards.sort(key=lambda c: c["severity"], reverse=True)
    return cards[:MAX_PER_PLAYER]


def _opp_team_batting(team_id: int) -> tuple[float, float]:
    """(season OPS, runs per game) for the opposing lineup. Same endpoint +
    cache_key the live pitcher-context feature build uses, so this is a
    cache hit whenever that ran first -- but deliberately fetched here
    rather than read from the model's feature dict, so trap detection has
    zero dependency on which feature schema is deployed."""
    data = stats_mlb._get(f"/teams/{team_id}/stats", {
        "stats": "season", "group": "hitting", "season": stats_mlb.SEASON,
    }, cache_key=f"team_batting_{team_id}_{stats_mlb.SEASON}")
    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return 0.0, 0.0
    s = splits[0].get("stat", {})
    games = int(s.get("gamesPlayed", 0) or 0)
    if not games:
        return 0.0, 0.0
    return _num(s.get("ops")), round(int(s.get("runs", 0) or 0) / games, 2)


def detect_pitcher_traps(job: dict, probs: dict) -> list:
    """Trap cards for one starting-pitcher job from build_board's slate scan."""
    games = fetch_current_season_gamelog(job["player_id"], "pitching")
    games = [g for g in games if g["stat"].get("gamesStarted") == 1]
    if len(games) < STREAK_WINDOW:
        return []
    if not job.get("opp_team_id"):
        return []

    opp_ops, opp_rpg = _opp_team_batting(job["opp_team_id"])
    hooks: list[tuple[str, str]] = []
    if opp_ops >= LINEUP_MIN_OPS:
        hooks.append(("LINEUP TRAP",
                      f"Tonight's opposing lineup is no pushover: {_fmt_avg(opp_ops)} team OPS"))
    if opp_rpg >= LINEUP_MIN_RPG:
        hooks.append(("LINEUP TRAP",
                      f"They're scoring {opp_rpg:.1f} runs per game this season"))
    if not hooks:
        return []

    cards = []
    for stat_type in _PITCHER_TRAP_STATS:
        over, window = _streak(games, stat_type, is_pitcher=True)
        if window >= STREAK_WINDOW and over >= BAIT_MIN_OVER:
            cards.append(_card(job, stat_type, over, window,
                               [h[1] for h in hooks], [h[0] for h in hooks],
                               probs.get(stat_type)))

    cards.sort(key=lambda c: c["severity"], reverse=True)
    return cards[:MAX_PER_PLAYER]


if __name__ == "__main__":
    # Standalone smoke test over today's REAL slate -- free MLB Stats API
    # only: no models loaded (model_prob shows as n/a), no Odds API credits.
    # Run from predictions-site/:  python -X utf8 v2/board/traps.py
    import v2.board.build_board as bb

    bb.predict_all = lambda feats: {}
    bb.predict_all_pitcher = lambda feats: {}
    _, bait_cards = bb.score_todays_slate()
    bait_cards.sort(key=lambda c: c["severity"], reverse=True)
    print(f"\n{len(bait_cards)} bait props detected on today's slate:\n")
    for b in bait_cards:
        model_txt = f"{b['model_prob']:.1%}" if b["model_prob"] is not None else "n/a (models not loaded)"
        print(f"[{b['trap_label']}] {b['player_name']} "
              f"({b['away_team_name']} @ {b['home_team_name']}) -- {b['bait']}")
        for h in b["hooks"]:
            print(f"    hook: {h}")
        print(f"    model to repeat: {model_txt}  |  streak implies: {b['naive_prob']:.0%}\n")
