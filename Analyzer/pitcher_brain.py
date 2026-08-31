"""Market-specific analyst model for MLB starting-pitcher strikeout props."""

from __future__ import annotations

from typing import Any


WEIGHTS = {
    "baseline": 25, "workload": 20, "opponent_k": 20,
    "arsenal_fit": 20, "command": 7, "opponent_quality": 6,
    "environment": 2,
}


def _f(value: Any, default: float = 0.0) -> float:
    try: return float(value)
    except (TypeError, ValueError): return default


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _side(over_score: float, side: str) -> float:
    return 100 - over_score if side == "under" else over_score


def evaluate(*, side: str, line: float, card: dict, pitcher_metrics: dict,
             pitcher_pitches: list, lineup_pitch_profile: dict,
             lineup_confirmed: bool, opponent_profile: dict,
             park: dict, weather: dict, team_history: dict | None = None,
             pitcher_is_home: bool | None = None, lineup_bvp: dict | None = None) -> dict:
    evidence = []

    def add(key: str, over_score: float, reliability: float, detail: str):
        evidence.append({"key": key, "weight": WEIGHTS[key],
                         "score": round(_side(_clamp(over_score), side), 1),
                         "reliability": round(_clamp(reliability, 0, 1), 2),
                         "detail": detail})

    splits = card.get("splits") or {}
    rates = []
    for key, weight in (("l5", .2), ("l10", .45), ("l20", .2)):
        row = splits.get(key) or {}
        if row.get("rate") is not None:
            rates.append((_f(row["rate"]), weight))
    rate_den = sum(weight for _, weight in rates)
    hit_rate = sum(rate * weight for rate, weight in rates) / rate_den if rate_den else 50
    season_avg = _f(splits.get("season_avg"))
    edge = season_avg - line
    baseline_over = hit_rate * .65 + _clamp(50 + edge * 10) * .35
    baseline_details = [f"Weighted recent Over rate {hit_rate:.0f}%", f"{season_avg:.1f} season K/start vs {line:g} line"]
    venue_key = "home" if pitcher_is_home is True else "away" if pitcher_is_home is False else None
    if venue_key:
        venue = card.get(f"{venue_key}_k_split") or {}
        venue_starts, venue_rate = int(_f(venue.get("starts"))), venue.get("over_rate")
        if venue_rate is not None and venue_starts >= 2:
            venue_conf = min(1.0, venue_starts / 10) ** .6
            venue_shift = _clamp((_f(venue_rate) - hit_rate) * .18, -7, 7) * venue_conf
            baseline_over += venue_shift
            baseline_details.append(f"{venue_key} {_f(venue.get('avg')):.1f} K/start in {venue_starts} starts ({venue_shift:+.1f})")
    team_values = (team_history or {}).get("current_values") or []
    if team_values:
        team_rate = sum(1 for value in team_values if value > line) / len(team_values) * 100
        team_avg = sum(team_values) / len(team_values)
        team_conf = min(1.0, len(team_values) / 5) ** .6
        team_shift = _clamp((team_rate - hit_rate) * .15, -6, 6) * team_conf
        baseline_over += team_shift
        baseline_details.append(f"current-season vs team {team_avg:.1f} K in {len(team_values)} start(s) ({team_shift:+.1f})")
    career_ip, career_k = _f((team_history or {}).get("career_ip")), _f((team_history or {}).get("career_k"))
    if career_ip >= 10:
        career_k9 = career_k / career_ip * 9
        season_k9 = _f(pitcher_metrics.get("k_per_9"), career_k9)
        career_conf = min(1.0, career_ip / 50) ** .6
        career_shift = _clamp((career_k9 - season_k9) * 1.2, -4, 4) * career_conf
        baseline_over += career_shift
        baseline_details.append(f"career vs team {career_k9:.1f} K/9 in {career_ip:.1f} IP ({career_shift:+.1f})")
    add("baseline", baseline_over, min(1.0, rate_den / .85),
        "; ".join(baseline_details))

    starts = card.get("last_5_starts") or []
    recent_ip = [int(s.get("outs", 0) or 0) / 3 for s in starts[:3] if s.get("outs") is not None]
    avg_ip = sum(recent_ip) / len(recent_ip) if recent_ip else 0
    avg_k = sum(_f(s.get("k")) for s in starts[:3]) / len(starts[:3]) if starts[:3] else 0
    workload_over = 50 + (avg_ip - 5.5) * 13 + (avg_k - line) * 4
    workload_risk = bool(pitcher_metrics.get("workload_risk"))
    if workload_risk: workload_over -= 10
    add("workload", workload_over, min(1.0, len(recent_ip) / 3),
        f"Last 3: {avg_ip:.1f} IP and {avg_k:.1f} K/start" + ("; long-rest/restriction risk" if workload_risk else ""))

    opp_k = card.get("opp_k") or {}
    opp_hand = card.get("opp_k_vs_hand") or {}
    overall_k = _f(opp_k.get("k_pct"), 22.5)
    hand_k = _f(opp_hand.get("k_pct"), overall_k)
    blended_k = hand_k * .65 + overall_k * .35
    opponent_k_over = 50 + (blended_k - 22.5) * 4
    lineup_ab, lineup_k_rate = int(_f((lineup_bvp or {}).get("ab"))), (lineup_bvp or {}).get("k_pct_ab")
    lineup_note = ""
    if lineup_k_rate is not None and lineup_ab >= 15:
        bvp_conf = min(1.0, lineup_ab / 100) ** .6
        bvp_shift = _clamp((_f(lineup_k_rate) - 22.5) * .8, -7, 7) * bvp_conf
        opponent_k_over += bvp_shift
        lineup_note = f"; confirmed lineup has {lineup_k_rate:.1f}% K/AB in {lineup_ab} career AB vs pitcher ({bvp_shift:+.1f})"
    opp_pa = int(_f(opp_hand.get("pa")))
    add("opponent_k", opponent_k_over, min(1.0, opp_pa / 300) ** .6 if opp_pa else .65,
        f"Opponent {overall_k:.1f}% K overall; {hand_k:.1f}% vs {card.get('hand', '?')}HP ({opp_pa or 'unknown'} PA){lineup_note}")

    pitch_num = pitch_den = put_num = 0.0
    lineup_num = lineup_den = 0.0
    labels = []
    for pitch in pitcher_pitches:
        usage, whiff, putaway = _f(pitch.get("pitch_usage") or pitch.get("pct")), _f(pitch.get("whiff_percent")), _f(pitch.get("put_away"))
        if usage <= 0: continue
        if whiff: pitch_num += whiff * usage; pitch_den += usage
        if putaway: put_num += putaway * usage
        lp = lineup_pitch_profile.get(pitch.get("pitch_type"), {})
        lineup_whiff = _f(lp.get("whiff_pct"))
        if lineup_whiff:
            lineup_num += lineup_whiff * usage; lineup_den += usage
        if usage >= 10:
            labels.append(f"{pitch.get('pitch_name', pitch.get('pitch_type'))} {usage:.0f}%/{whiff:.1f}% whiff")
    pitcher_whiff = pitch_num / pitch_den if pitch_den else 0
    pitcher_putaway = put_num / pitch_den if pitch_den else 0
    lineup_whiff = lineup_num / lineup_den if lineup_den else 0
    arsenal_over = 50
    if pitcher_whiff: arsenal_over += (pitcher_whiff - 25) * 1.5
    if pitcher_putaway: arsenal_over += (pitcher_putaway - 20) * .7
    if lineup_whiff: arsenal_over += (lineup_whiff - 25) * 1.2
    arsenal_rel = min(1.0, pitch_den / 75) * (1.0 if lineup_confirmed and lineup_den >= 50 else .55)
    detail = f"Pitcher mix {pitcher_whiff:.1f}% whiff/{pitcher_putaway:.1f}% putaway"
    detail += f"; confirmed lineup {lineup_whiff:.1f}% whiff vs mix" if lineup_whiff else "; lineup pitch profile unavailable"
    if labels: detail += "; " + ", ".join(labels)
    add("arsenal_fit", arsenal_over, arsenal_rel, detail)

    bb9 = _f(pitcher_metrics.get("bb_per_9"), 3.2)
    opp_bb = _f(opponent_profile.get("bb_pct"), 8.5)
    command_over = 50 - (bb9 - 3.2) * 5 - (opp_bb - 8.5) * 1.2
    add("command", command_over, .85, f"Pitcher {bb9:.2f} BB/9; opponent {opp_bb:.1f}% BB rate")

    opp_avg = _f(opponent_profile.get("avg"), .250)
    opp_wrc = _f(opponent_profile.get("wrc_plus"), 100)
    quality_over = 50 - (opp_avg - .250) * 100 - (opp_wrc - 100) * .12
    add("opponent_quality", quality_over, 1.0 if opponent_profile else 0,
        f"Opponent {opp_avg:.3f} AVG; offense index {opp_wrc:.0f}")

    # Run park and wind have little demonstrated direct effect on strikeouts;
    # keep this deliberately tiny rather than copying a hitter/HR adjustment.
    weather_valid = bool(weather and not weather.get("error") and weather.get("temp_f") is not None)
    temp = _f(weather.get("temp_f"), 70) if weather_valid else 70
    environment_over = 50 - (temp - 70) * .05 if weather_valid else 50
    weather_detail = f"{temp:.0f}°F" if weather_valid else "weather unavailable"
    add("environment", environment_over, .4 if weather_valid else 0,
        f"{park.get('name', 'Unknown park')}; {weather_detail} — minimal K influence")

    usable = sum(e["weight"] * e["reliability"] for e in evidence)
    weighted = sum(e["score"] * e["weight"] * e["reliability"] for e in evidence) / usable if usable else 50
    coverage = usable / 100
    score = 50 + (weighted - 50) * (.55 + .45 * coverage)
    pos = sum(1 for e in evidence if e["score"] >= 62 and e["reliability"] >= .5)
    neg = sum(1 for e in evidence if e["score"] <= 38 and e["reliability"] >= .5)
    conflicts = min(pos, neg)
    score = round(_clamp(score - conflicts * 1.5))
    usable_factors = [e for e in evidence if e["reliability"] >= .35 and e["weight"] * e["reliability"] >= 2]
    supporting = [e for e in usable_factors if e["score"] >= 56]
    opposing = [e for e in usable_factors if e["score"] <= 44]
    neutral = [e for e in usable_factors if 44 < e["score"] < 56]
    directional_weight = sum(e["weight"] * e["reliability"] for e in supporting + opposing)
    agreement = (sum(e["weight"] * e["reliability"] for e in supporting) / directional_weight
                 if directional_weight else .5)
    if len(supporting) < 2: score = min(score, 59)
    elif len(supporting) < 3: score = min(score, 64)
    if len(supporting) < 4 or agreement < .62: score = min(score, 74)
    if len(supporting) < 5 or agreement < .72: score = min(score, 84)
    label = "ELITE" if score >= 85 else "STRONG" if score >= 75 else "LEAN" if score >= 65 else "NEUTRAL" if score >= 55 else "AVOID"
    evidence.sort(key=lambda e: abs(e["score"] - 50) * e["weight"] * e["reliability"], reverse=True)
    return {"score": score, "label": label, "coverage": round(coverage, 2),
            "conflicts": conflicts, "evidence": evidence,
            "consensus": {"supporting": len(supporting), "opposing": len(opposing),
                          "neutral": len(neutral), "usable": len(usable_factors),
                          "agreement": round(agreement, 2)}}
