"""Market-specific, sample-aware reasoning for standalone MLB hitter props."""

from __future__ import annotations

import math
from typing import Any


MARKET_WEIGHTS = {
    "hits":            {"baseline": 25, "hand": 13, "pitch_mix": 13, "pitcher": 12, "contact": 12, "role": 10, "team": 4,  "bullpen": 5, "environment": 3, "bvp": 3},
    "total_bases":     {"baseline": 20, "hand": 10, "pitch_mix": 15, "pitcher": 12, "contact": 17, "role": 7,  "team": 4,  "bullpen": 4, "environment": 8, "bvp": 3},
    "home_runs":      {"baseline": 14, "hand": 8,  "pitch_mix": 17, "pitcher": 14, "contact": 22, "role": 5,  "team": 3,  "bullpen": 3, "environment": 11,"bvp": 3},
    "rbis":           {"baseline": 18, "hand": 8,  "pitch_mix": 10, "pitcher": 10, "contact": 10, "role": 16, "team": 15, "bullpen": 5, "environment": 5, "bvp": 3},
    "runs_scored":    {"baseline": 18, "hand": 8,  "pitch_mix": 8,  "pitcher": 9,  "contact": 8,  "role": 17, "team": 18, "bullpen": 5, "environment": 6, "bvp": 3},
    "hits_runs_rbis": {"baseline": 26, "hand": 9,  "pitch_mix": 11, "pitcher": 10, "contact": 7,  "role": 12, "team": 10, "bullpen": 5, "environment": 6, "bvp": 4},
    "walks":          {"baseline": 30, "hand": 7,  "pitch_mix": 5,  "pitcher": 18, "contact": 4,  "role": 14, "team": 8,  "bullpen": 10,"environment": 2, "bvp": 2},
    "strikeouts":     {"baseline": 23, "hand": 11, "pitch_mix": 14, "pitcher": 16, "contact": 13, "role": 10, "team": 4,  "bullpen": 5, "environment": 1, "bvp": 3},
    "fantasy_score":  {"baseline": 21, "hand": 9,  "pitch_mix": 11, "pitcher": 10, "contact": 13, "role": 12, "team": 9,  "bullpen": 5, "environment": 6, "bvp": 4},
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _side(score_over: float, side: str) -> float:
    return 100 - score_over if side == "under" else score_over


def _empirical(splits: dict, side: str) -> tuple[float, float, str]:
    rows, total = [], 0.0
    for key, weight in (("l5", .15), ("l10", .40), ("l20", .30)):
        row = splits.get(key) or {}
        if row.get("rate") is None:
            continue
        rate = _f(row["rate"])
        rows.append((_side(rate, side), weight)); total += weight
    season_avg, line = _f(splits.get("season_avg")), _f(splits.get("_line"))
    value = sum(rate * weight for rate, weight in rows) / total if total else 50
    form = splits.get("recent_batting_form") or {}
    form_delta = _f(form.get("delta_pct"))
    if form.get("delta_pct") is not None:
        # Recent OPS is confirmation, not another independent full-strength
        # signal. Cap it so ten hot/cold games cannot erase the longer sample.
        adjustment = _clamp(form_delta * .35, -8, 8)
        value += adjustment if side == "over" else -adjustment
        detail = (f"Recent side-adjusted hit rate {value - (adjustment if side == 'over' else -adjustment):.0f}%; "
                  f"L10 OPS trend {form_delta:+.1f}%; season average {season_avg:.2f}")
    else:
        detail = f"Recent side-adjusted hit rate {value:.0f}%; season average {season_avg:.2f}"
    return value, min(1.0, total / .85), detail


def _contact_score(statcast: dict, prop: str) -> tuple[float, float, str]:
    sc = statcast or {}
    barrel, hard = _f(sc.get("barrel_pct")), _f(sc.get("hard_hit_pct"))
    xslg, xwoba = _f(sc.get("xslg")), _f(sc.get("xwoba"))
    whiff, chase = _f(sc.get("whiff_pct")), _f(sc.get("chase_pct"))
    values = []
    if prop in {"home_runs", "total_bases", "fantasy_score", "hits_runs_rbis", "rbis"}:
        if barrel: values.append(50 + (barrel - 7.5) * 3.0)
        if hard: values.append(50 + (hard - 39) * 1.2)
        if xslg: values.append(50 + (xslg - .410) * 100)
        pieces = []
        if barrel: pieces.append(f"{barrel:.1f}% barrels")
        if hard: pieces.append(f"{hard:.1f}% hard-hit")
        if xslg: pieces.append(f"{xslg:.3f} xSLG")
        detail = ", ".join(pieces) or "Power-contact data unavailable"
    elif prop == "strikeouts":
        if whiff: values.append(50 + (whiff - 25) * 2.0)
        if chase: values.append(50 + (chase - 29) * 1.2)
        detail = f"{whiff:.1f}% whiff, {chase:.1f}% chase"
    else:
        if xwoba: values.append(50 + (xwoba - .320) * 100)
        if hard: values.append(50 + (hard - 39) * .8)
        detail = f"{hard:.1f}% hard-hit, {xwoba:.3f} xwOBA"
    return ((sum(values) / len(values), min(1.0, len(values) / 2), detail)
            if values else (50, 0, "Contact-quality data unavailable"))


def _role_score(lineup_spot: int | None, prop: str) -> tuple[float, float, str]:
    if not lineup_spot:
        return 50, 0.0, "Lineup not confirmed; no role edge credited"
    pa = {1: 4.65, 2: 4.55, 3: 4.4, 4: 4.3, 5: 4.1, 6: 3.95, 7: 3.8, 8: 3.7, 9: 3.55}.get(lineup_spot, 4.0)
    score = 50 + (pa - 4.05) * 30
    if prop == "rbis": score += 10 if lineup_spot in (3, 4, 5) else -7 if lineup_spot in (1, 8, 9) else 0
    if prop == "runs_scored": score += 9 if lineup_spot in (1, 2, 3) else -7 if lineup_spot >= 7 else 0
    return _clamp(score), 1.0, f"Batting {lineup_spot}{'st' if lineup_spot == 1 else 'nd' if lineup_spot == 2 else 'rd' if lineup_spot == 3 else 'th'}; about {pa:.1f} expected PA"


def evaluate(*, prop: str, side: str, line: float, splits: dict, matchup_factors: list,
             statcast: dict, lineup_spot: int | None, team_profile: dict,
             bullpen: dict, park: dict, weather: dict, bvp: dict,
             pitch_profile: list | None = None, arsenal: list | None = None,
             home_away: dict | None = None, batter_is_home: bool | None = None,
             pitcher_venue: dict | None = None, pitcher_season_era: float | None = None) -> dict:
    """Return a market-specific confidence score and auditable reasoning."""
    weights = MARKET_WEIGHTS.get(prop, MARKET_WEIGHTS["hits"])
    split_copy = dict(splits or {}); split_copy["_line"] = line
    matchup = {f.get("key"): f for f in matchup_factors or []}
    evidence = []

    def add(key: str, over_score: float, reliability: float, detail: str):
        evidence.append({"key": key, "weight": weights[key],
                         "score": round(_side(_clamp(over_score), side), 1),
                         "reliability": round(_clamp(reliability, 0, 1), 2), "detail": detail})

    empirical, rel, detail = _empirical(split_copy, side)
    venue_key = "home" if batter_is_home is True else "away" if batter_is_home is False else None
    if venue_key:
        venue_avg = _f((home_away or {}).get(f"{venue_key}_avg"), -1)
        venue_games = int(_f((home_away or {}).get(f"{venue_key}_games")))
        season_avg = _f(splits.get("season_avg"))
        if venue_avg >= 0 and season_avg > 0 and venue_games >= 4:
            venue_conf = min(1.0, venue_games / 20) ** .6
            venue_shift = _clamp((venue_avg - season_avg) / max(line, 1) * 8, -8, 8) * venue_conf
            empirical += venue_shift if side == "over" else -venue_shift
            detail += f"; {venue_key} average {venue_avg:.2f} in {venue_games} games ({venue_shift:+.1f} venue adjustment)"
    # Empirical is already side-adjusted; undo before add() applies direction.
    add("baseline", empirical if side == "over" else 100 - empirical, rel, detail)
    for local_key, matchup_key in (("hand", "handedness"),
                                   ("pitcher", "pitcher_quality"), ("bvp", "bvp")):
        f = matchup.get(matchup_key) or {}
        sided_score = _f(f.get("score"), 50)
        over_score = sided_score if side == "over" else 100 - sided_score
        if local_key == "hand":
            # Handedness is an adjustment to the hitter baseline, not a second
            # projection. Limit even huge splits to a 20-point lean.
            over_score = 50 + _clamp(over_score - 50, -20, 20)
        if local_key == "pitcher" and batter_is_home is not None:
            pitcher_key = "away" if batter_is_home else "home"
            venue_era = _f((pitcher_venue or {}).get(f"{pitcher_key}_era"))
            venue_ip = _f((pitcher_venue or {}).get(f"{pitcher_key}_ip"))
            season_era = _f(pitcher_season_era)
            if venue_era > 0 and season_era > 0 and venue_ip >= 5:
                venue_conf = min(1.0, venue_ip / 40) ** .6
                venue_shift = _clamp((venue_era - season_era) * 5, -8, 8) * venue_conf
                over_score += venue_shift
                f = dict(f)
                f["detail"] = (f.get("detail", "") +
                               f"; {pitcher_key} ERA {venue_era:.2f} in {venue_ip:.1f} IP ({venue_shift:+.1f} venue adjustment)")
        # BvP is deliberately capped: career history informs, never drives.
        reliability = _f(f.get("confidence"), 0) * (.65 if local_key == "bvp" else 1.0)
        add(local_key, over_score, reliability, f.get("detail", "Data unavailable"))

    # Arsenal FIT must compare the pitcher's mix with the hitter's own overall
    # pitch profile. Comparing Yordan's .470 mix wOBA only with league average
    # incorrectly credits his general talent a second time.
    profile = {row.get("pitch_type"): row for row in (pitch_profile or [])}
    overall_num = overall_den = mix_num = mix_den = 0.0
    labels = []
    for row in profile.values():
        woba, pitches = _f(row.get("woba")), _f(row.get("pitches"))
        if woba and pitches:
            overall_num += woba * pitches; overall_den += pitches
    for pitch in (arsenal or []):
        row = profile.get(pitch.get("pitch_type"))
        woba, usage = _f((row or {}).get("woba")), _f(pitch.get("pct"))
        if woba and usage:
            mix_num += woba * usage; mix_den += usage
            if usage >= 10: labels.append(f"{pitch.get('pitch_name', pitch.get('pitch_type'))} {usage:.0f}%/{woba:.3f}")
    if overall_den and mix_den:
        overall_woba, mix_woba = overall_num / overall_den, mix_num / mix_den
        fit_delta = mix_woba - overall_woba
        mix_over = 50 + _clamp(fit_delta * 180, -22, 22)
        add("pitch_mix", mix_over, min(1.0, mix_den / 75),
            f"{mix_woba:.3f} matchup-mix wOBA vs {overall_woba:.3f} hitter baseline ({fit_delta:+.3f}); " + ", ".join(labels))
    else:
        add("pitch_mix", 50, 0, "Pitch-mix fit unavailable")

    contact, rel, detail = _contact_score(statcast, prop)
    add("contact", contact, rel, detail)
    role, rel, detail = _role_score(lineup_spot, prop)
    add("role", role, rel, detail)

    runs_pg, wrc = _f(team_profile.get("runs_pg"), 4.4), _f(team_profile.get("wrc_plus"), 100)
    team_over = 50 + (runs_pg - 4.4) * 7 + (wrc - 100) * .18
    add("team", team_over, 1.0 if team_profile else 0, f"Team scores {runs_pg:.2f} runs/game; offense index {wrc:.0f}")

    bp_era = _f(bullpen.get("era_f") or bullpen.get("era"), 4.3)
    bp_usable = bool(bullpen.get("model_usable"))
    bp_detail = (f"Opposing bullpen {bp_era:.2f} ERA ({bullpen.get('sample', 'unknown')} sample)"
                 if bp_usable else "Reliable relief-only bullpen sample unavailable; no edge credited")
    add("bullpen", 50 + (bp_era - 4.3) * 7, .75 if bp_usable else 0, bp_detail)

    pf = _f(park.get("factor"), 1.0)
    env = 50 + (pf - 1) * (350 if prop in {"home_runs", "total_bases"} else 180)
    if not weather.get("dome"):
        temp = _f(weather.get("temp_f"), 70); wind = _f(weather.get("speed_mph"))
        env += (temp - 70) * (.35 if prop in {"home_runs", "total_bases"} else .15)
        if weather.get("hitter_friendly") is True: env += min(10, wind * .7)
        elif weather.get("hitter_friendly") is False: env -= min(10, wind * .7)
    park_reliability = _f(park.get("source_reliability"), .5 if park else .0)
    weather_reliability = .8 if weather and not weather.get("error") else 0
    env_reliability = min(.7, park_reliability * .7 + weather_reliability * .3)
    add("environment", env, env_reliability,
        f"{park.get('name', 'Unknown park')} estimated factor {pf:.2f}; "
        f"{weather.get('effect', 'weather unavailable')} (wind effect is modeled)")

    usable_weight = sum(e["weight"] * e["reliability"] for e in evidence)
    weighted = (sum(e["score"] * e["weight"] * e["reliability"] for e in evidence) / usable_weight
                if usable_weight else 50)
    coverage = usable_weight / sum(weights.values())
    # Thin data must move the grade toward neutral, never manufacture certainty.
    score = 50 + (weighted - 50) * (0.55 + 0.45 * coverage)
    strong_pos = sum(1 for e in evidence if e["score"] >= 62 and e["reliability"] >= .45)
    strong_neg = sum(1 for e in evidence if e["score"] <= 38 and e["reliability"] >= .45)
    conflict = min(strong_pos, strong_neg)
    if conflict: score -= min(5, conflict * 1.5)
    # A human analyst does not promote a play from one standout statistic.
    # Require agreement across independent, usable categories before allowing
    # LEAN/STRONG/ELITE grades. Neutral categories still count as consumed data,
    # but they do not count as confirmation.
    usable = [e for e in evidence if e["reliability"] >= .35 and e["weight"] * e["reliability"] >= 2]
    supporting = [e for e in usable if e["score"] >= 56]
    opposing = [e for e in usable if e["score"] <= 44]
    neutral = [e for e in usable if 44 < e["score"] < 56]
    directional_weight = sum(e["weight"] * e["reliability"] for e in supporting + opposing)
    agreement = (sum(e["weight"] * e["reliability"] for e in supporting) / directional_weight
                 if directional_weight else .5)
    if len(supporting) < 2:
        score = min(score, 59)
    elif len(supporting) < 3:
        score = min(score, 64)
    if len(supporting) < 4 or agreement < .62:
        score = min(score, 74)
    if len(supporting) < 5 or agreement < .72:
        score = min(score, 84)
    score = round(_clamp(score))
    label = "ELITE" if score >= 85 else "STRONG" if score >= 75 else "LEAN" if score >= 65 else "NEUTRAL" if score >= 55 else "AVOID"
    context_rows = [e for e in evidence if e["key"] in
                    {"hand", "pitch_mix", "pitcher", "contact", "bullpen", "environment", "bvp"}
                    and e["reliability"] > 0]
    context_den = sum(e["weight"] * e["reliability"] for e in context_rows)
    context_score = round(sum(e["score"] * e["weight"] * e["reliability"] for e in context_rows) / context_den) if context_den else 50
    context_label = ("Strong" if context_score >= 67 else "Favorable" if context_score >= 58 else
                     "Neutral" if context_score >= 43 else "Unfavorable" if context_score >= 34 else "Poor")
    evidence.sort(key=lambda e: abs(e["score"] - 50) * e["weight"] * e["reliability"], reverse=True)
    return {"score": score, "label": label, "coverage": round(coverage, 2),
            "conflicts": conflict, "evidence": evidence,
            "consensus": {"supporting": len(supporting), "opposing": len(opposing),
                          "neutral": len(neutral), "usable": len(usable),
                          "agreement": round(agreement, 2)},
            "context_score": context_score, "context_label": context_label,
            "method": "market-specific opportunity × rate × context model"}
