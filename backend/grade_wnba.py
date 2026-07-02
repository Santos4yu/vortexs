"""
Vortex — WNBA Prop Scoring Engine
==================================
Basketball props are driven by different signals than baseball. There is no
pitcher/park/handedness; instead the dominant factors are:

  1. Minutes floor & stability   — a prop can't hit if the player isn't on the
                                    court. This is the single biggest WNBA gate.
  2. Projection edge             — L10 average vs the line (market mispricing).
  3. Recent + base-rate form     — L5 / L10 / L20 hit rates.
  4. Pace matchup                — opponent + own pace → possession volume.
  5. Consistency (variance)      — low stdev = reliable; boom/bust = size down.

grade_pick mirrors the MLB engine's interface (returns the same dict shape:
score / label / emoji / color / proj_edge) so the board + embed code can treat
both sports uniformly. Tier thresholds match MLB:
  💎 Elite ≥10 · 🔥 Strong 6-9 · ✅ Good 3-5 · ➡️ Lean 0-2 · ⚠️ Risky <0
"""

import statistics

# Discord embed colors (ints) — reuse MLB palette for visual consistency.
_COLOR = {
    "Elite":  0x2ECC71, "Strong": 0x3498DB, "Good": 0xF1C40F,
    "Lean":   0x95A5A6, "Risky":  0xE67E22, "Fade": 0xE74C3C,
}
_EMOJI = {
    "Elite": "💎", "Strong": "🔥", "Good": "✅",
    "Lean": "➡️", "Risky": "⚠️", "Fade": "🚫",
}


def _label_for(score: int) -> str:
    if   score >= 10: return "Elite"
    elif score >= 6:  return "Strong"
    elif score >= 3:  return "Good"
    elif score >= 0:  return "Lean"
    elif score >= -4: return "Risky"
    else:             return "Fade"


def grade_pick(
    splits: dict,
    line: float,
    side: str = "over",
    prop_type: str = "",
    team_pace: float | None = None,
    opp_pace: float | None = None,
    league_pace: float | None = None,
    opp_def_rank: int | None = None,    # 1 = stingiest defense … N = most generous (vs this stat)
    n_teams: int = 15,
    game_flags: list | None = None,     # e.g. ["b2b", "blowout_risk"]
    teammate_out: str | None = None,    # name of a starter-level teammate who is OUT
    self_status: str | None = None,     # "out" | "questionable" for THIS player
    learned_weight: float | None = None,
) -> dict:
    """
    Score a WNBA prop. `splits` is the dict from stats_wnba.get_historical_splits.

    Implements the 4-filter funnel as graded factors + hard guards:
      Filter 1 Role/Volume  → minutes gate + consistency (L10 vs season ±15%)
      Filter 2 Matchup      → opp_def_rank (defense vs this stat) + pace
      Filter 3 Line Value   → projection edge, with a sharp-money guard on
                              implausibly large gaps (market knows something)
      Filter 4 Game Script  → game_flags (back-to-back, blowout risk)
    """
    is_under = side.lower() == "under"

    l5  = splits.get("l5")  or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}
    has_l10 = bool(l10.get("games", 0))
    has_l20 = bool(l20.get("games", 0))

    l5_rate  = l5.get("rate", 0)  or 0
    l10_rate = l10.get("rate", 0) or 0
    l20_rate = l20.get("rate", 0) or 0
    l10_avg  = l10.get("avg", 0)  or 0

    eff_l5  = (100 - l5_rate)  if is_under else l5_rate
    eff_l10 = (100 - l10_rate) if is_under else l10_rate
    eff_l20 = (100 - l20_rate) if is_under else l20_rate

    score = 0

    # ── 1. Recent form (streaks support, don't decide) ───────────────────────
    if has_l10:
        if   eff_l10 >= 90: score += 4
        elif eff_l10 >= 70: score += 2
    if l5.get("games"):
        if   eff_l5 == 100: score += 2
        elif eff_l5 >= 80:  score += 1
    if has_l20 and eff_l20 >= 75:
        score += 2

    # ── 2. Projection edge — L10 average vs line (strongest EV signal) ────────
    # Filter 3: a normal edge is good, but an *implausibly* large gap (line sitting
    # far from recent output) usually means the market knows something we don't
    # (injury, role change, rest) — so we cap the reward and flag it, rather than
    # treating a 3+ gap as free money.
    proj_edge = 0.0
    sharp_flag = False
    if line > 0 and l10_avg:
        raw = float(l10_avg) - float(line)
        proj_edge = -raw if is_under else raw
        # "too good to be true" threshold — scaled to line magnitude
        suspicious = abs(raw) >= max(3.0, 0.40 * line)
        if suspicious and proj_edge > 0:
            score += 1          # capped credit instead of full +3
            sharp_flag = True   # market disagrees with the raw average → caution
        elif proj_edge >= 3.0:  score += 3
        elif proj_edge >= 1.5:  score += 2
        elif proj_edge >= 0.5:  score += 1
        elif proj_edge <= -3.0: score -= 3
        elif proj_edge <= -1.5: score -= 2
        elif proj_edge <= -0.5: score -= 1

    # ── Filter 1 consistency: L10 average should be near the season average ──
    # A big L10-vs-season divergence means the recent window is a hot/cold blip
    # that will regress — reduce confidence in the recent number.
    season_avg = splits.get("season_avg") or 0
    consistency_flag = False
    if season_avg and l10_avg:
        drift = abs(l10_avg - season_avg) / season_avg
        if drift > 0.15:        # >15% off the season baseline
            score -= 1
            consistency_flag = True

    # ── 3. Minutes floor & stability — the key WNBA gate ─────────────────────
    # A high, stable minutes load makes any counting-stat prop more reliable.
    # Low or collapsing minutes caps the ceiling regardless of hit rate.
    min_l10 = splits.get("min_l10") or 0
    min_l5  = splits.get("min_l5")  or 0
    minutes_flag = None
    if min_l10:
        if not is_under:
            if   min_l10 >= 30: score += 3   # heavy starter load
            elif min_l10 >= 26: score += 2
            elif min_l10 >= 20: score += 1
            elif min_l10 < 16:  score -= 2; minutes_flag = "low_minutes"
        else:
            # Under benefits from a LOW / shrinking minutes role
            if   min_l10 < 18:  score += 2
            elif min_l10 < 24:  score += 1
            elif min_l10 >= 32: score -= 1
        # Trend: minutes rising/falling sharply (role change)
        if min_l5 and min_l10:
            delta = min_l5 - min_l10
            if not is_under and delta >= 4:   score += 1   # role expanding
            if not is_under and delta <= -5:  score -= 2; minutes_flag = "minutes_dropping"
            if is_under and delta <= -4:      score += 1   # role shrinking helps Under

    # ── 4. Pace matchup — possession volume drives counting stats ────────────
    # Opponent pace matters most (it sets the game's possessions for both teams).
    if opp_pace and league_pace:
        pace_ratio = opp_pace / league_pace
        if not is_under:
            if   pace_ratio >= 1.06: score += 2   # fast opponent → more volume
            elif pace_ratio >= 1.02: score += 1
            elif pace_ratio <= 0.94: score -= 2   # slow grind-it-out opponent
            elif pace_ratio <= 0.98: score -= 1
        else:
            if   pace_ratio <= 0.94: score += 2
            elif pace_ratio <= 0.98: score += 1
            elif pace_ratio >= 1.06: score -= 2
            elif pace_ratio >= 1.02: score -= 1

    # ── 4b. Opponent defense vs THIS stat (Filter 2) ─────────────────────────
    # opp_def_rank: 1 = stingiest (gives up least) … n_teams = most generous.
    # Facing a bottom-tier defense in this stat is a green light for the Over.
    if opp_def_rank is not None and n_teams >= 5:
        pct = (opp_def_rank - 1) / (n_teams - 1)   # 0 = best D, 1 = worst D
        if not is_under:
            if   pct >= 0.80: score += 3   # bottom-3 defense — prime spot
            elif pct >= 0.60: score += 2
            elif pct >= 0.45: score += 1
            elif pct <= 0.15: score -= 3   # top-3 defense — fade the Over
            elif pct <= 0.35: score -= 1
        else:
            if   pct <= 0.15: score += 3   # elite D suppresses → Under
            elif pct <= 0.35: score += 1
            elif pct >= 0.80: score -= 3
            elif pct >= 0.60: score -= 1

    # ── 4c. Lineup context (Filter 5) — injuries redistribute usage ──────────
    # A starter-level teammate being OUT bumps the remaining player's usage and
    # shot volume → helps the Over. This is the well-supported version of "lineup
    # context"; we deliberately do NOT try to model per-teammate historical splits
    # (tiny samples in a 40-game season — that's overfitting, not signal).
    lineup_flag = None
    if teammate_out:
        if not is_under:
            score += 2
        else:
            score -= 1
        lineup_flag = "teammate_out"
    # This player's own status — uncertain minutes is a real risk on counting props.
    self_flag = None
    if self_status == "questionable":
        score -= 1
        self_flag = "minutes_uncertain"
    # ("out" is handled upstream in enrichment — the prop is dropped entirely.)

    # ── 5. Consistency / variance ────────────────────────────────────────────
    stability_tier = ""
    recent_vals = [g["value"] for g in (splits.get("recent_games") or [])
                   if isinstance(g.get("value"), (int, float))]
    if len(recent_vals) >= 5:
        stdev = statistics.stdev(recent_vals)
        # Scale thresholds by line magnitude — a points prop (line ~18) is
        # naturally more variable than a threes prop (line ~1.5).
        rel = stdev / line if line else stdev
        if   rel < 0.30: stability_tier = "HIGH";     score += 1
        elif rel < 0.55: stability_tier = "MEDIUM"
        elif rel < 0.85: stability_tier = "LOW";      score -= 1
        else:            stability_tier = "VOLATILE"; score -= 2

    # ── 6. Low-sample penalty ────────────────────────────────────────────────
    l10_games = l10.get("games", 0) or 0
    if   l10_games < 5:  score -= 3
    elif l10_games < 10: score -= 2
    elif not has_l20:    score -= 1

    # ── Learned weight modifier (parity with MLB pipeline) ───────────────────
    if learned_weight is not None and learned_weight != 1.0:
        score = round(score * learned_weight)

    # ── Filter 4: Game-script red flags (back-to-back, blowout risk) ──────────
    game_flags = game_flags or []
    for gf in game_flags:
        if gf == "b2b":
            score -= 2            # fatigue / minutes management on a back-to-back
        elif gf == "blowout_risk":
            # Lopsided spread → starters may sit in the 4th → caps Over volume
            if not is_under:
                score -= 2
            else:
                score += 1        # blowout can help Unders (bench time)

    # ── Risk caps ────────────────────────────────────────────────────────────
    risk_flags = []
    if minutes_flag:
        risk_flags.append(minutes_flag)
    if self_flag:
        risk_flags.append(self_flag)        # this player's minutes uncertain
    if sharp_flag:
        risk_flags.append("sharp_line")     # market disagrees with recent avg
    if consistency_flag:
        risk_flags.append("inconsistent")   # L10 diverges from season baseline
    if l5.get("games") and eff_l5 <= 40:
        score -= 4
        risk_flags.append("cold_form")
    if stability_tier == "VOLATILE":
        risk_flags.append("volatile")
    for gf in game_flags:
        risk_flags.append(gf)

    label = _label_for(score)
    # A sharp line (market far from our number) alone caps confidence at Strong —
    # we don't stamp Elite on a spot the market is screaming against.
    if "sharp_line" in risk_flags and label == "Elite":
        label = "Strong"
    # Two+ risk flags → cap at Good (can't be Elite/Strong on a flagged spot).
    # Computed on negative flags only — positive context (teammate_out) is added
    # to the displayed flags afterward so it never triggers a downgrade.
    if len(risk_flags) >= 2 and label in ("Elite", "Strong"):
        label = "Good"

    display_flags = list(risk_flags)
    if lineup_flag:
        display_flags.append(lineup_flag)

    return {
        "score":      int(round(score)),
        "label":      label,
        "emoji":      _EMOJI[label],
        "color":      _COLOR[label],
        "proj_edge":  round(proj_edge, 2),
        "stability":  stability_tier,
        "risk_flags": display_flags,
        "minutes_l10": min_l10,
    }


def grade_pick_both(splits, line, prop_type="", **kw) -> dict:
    """Grade both sides; return model verdict + both grades (mirrors MLB)."""
    over_g  = grade_pick(splits, line, side="over",  prop_type=prop_type, **kw)
    under_g = grade_pick(splits, line, side="under", prop_type=prop_type, **kw)
    o, u = over_g["score"], under_g["score"]
    verdict = "over" if o >= u else "under"
    conf = min(0.90, max(0.5, 0.5 + abs(o - u) / 24))   # cap at 90% — no prop is a lock
    return {
        "model_verdict": verdict,
        "over_score": o, "under_score": u,
        "over_grade": over_g, "under_grade": under_g,
        "confidence": round(conf, 2),
    }
