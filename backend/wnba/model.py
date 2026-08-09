"""Minutes/opportunity-first WNBA player-prop probability model."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import mean, median, pstdev

MODEL_VERSION = "wnba-v1.1-context"

PROP_VARIANCE = {
    "points": 1.00, "rebounds": .82, "assists": .90, "threes": 1.28,
    "steals": 1.45, "blocks": 1.48, "pts_reb": .88, "pts_ast": .90,
    "reb_ast": .88, "pts_reb_ast": .85,
}


@dataclass(slots=True)
class WNBAInput:
    player_id: str
    player_name: str
    team: str
    opponent: str
    game_date: str
    commence_time: str
    market_key: str
    prop_type: str
    side: str
    line: float
    over_odds: int | None
    under_odds: int | None
    best_book: str
    projected_minutes: float
    season_minutes: float
    recent_minutes: list[float]
    recent_values: list[float]
    season_rate_per_minute: float
    recent_rate_per_minute: float | None = None
    is_home: bool | None = None
    venue_factor: float = 1.0
    venue_sample: int = 0
    h2h_factor: float = 1.0
    h2h_sample: int = 0
    role: str = "unknown"
    availability: str = "active"
    minutes_restriction: bool = False
    role_confirmed: bool = False
    opponent_factor: float = 1.0
    pace_factor: float = 1.0
    lineup_factor: float = 1.0
    rest_factor: float = 1.0
    game_environment_factor: float = 1.0
    usage_factor: float = 1.0
    efficiency_regression: float = 1.0
    spread: float | None = None
    game_total: float | None = None
    books_count: int = 1
    injury_data: bool = False
    opponent_data: bool = False
    tracking_data: bool = False
    likely_defender_data: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WNBAEvaluation:
    model_version: str
    tier: str
    publish: bool
    watchlist: bool
    projected_mean: float
    projected_median: float
    projected_floor: float
    projected_ceiling: float
    standard_deviation: float
    over_probability: float
    under_probability: float
    selected_probability: float
    market_probability: float | None
    edge_pp: float | None
    fair_odds: int
    data_quality: int
    variance_score: int
    variance_label: str
    minutes_confidence: int
    reasons: list[str]
    risks: list[str]
    hard_rejections: list[str]
    diagnostics: dict

    def as_dict(self) -> dict:
        return asdict(self)


def american_probability(odds: int | None) -> float | None:
    if odds is None or odds == 0:
        return None
    return abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)


def no_vig_probability(over_odds: int | None, under_odds: int | None, side: str) -> float | None:
    over, under = american_probability(over_odds), american_probability(under_odds)
    if over is None or under is None or over + under <= 0:
        return None
    p_over = over / (over + under)
    return p_over if side == "over" else 1 - p_over


def probability_to_american(probability: float) -> int:
    p = min(.995, max(.005, probability))
    return round(-100 * p / (1 - p)) if p >= .5 else round(100 * (1 - p) / p)


def _normal_cdf(value: float, mu: float, sigma: float) -> float:
    return .5 * (1 + math.erf((value - mu) / (max(.01, sigma) * math.sqrt(2))))


def _shrink(value: float, baseline: float, sample: int, target: int) -> float:
    weight = min(1.0, max(0, sample) / target)
    return baseline + (value - baseline) * weight


def _quality(x: WNBAInput) -> tuple[int, list[str]]:
    score, missing = 15, []  # verified identity/market fields are constructor requirements
    if x.availability.lower() in {"active", "probable"}: score += 15
    else: missing.append("availability uncertain")
    if x.role_confirmed: score += 15
    else: missing.append("starting role not confirmed")
    if len(x.recent_minutes) >= 8: score += 15
    elif len(x.recent_minutes) >= 5: score += 10
    else: missing.append("limited minutes history")
    if len(x.recent_values) >= 10: score += 10
    elif len(x.recent_values) >= 5: score += 6
    else: missing.append("limited prop history")
    if x.opponent_data: score += 10
    else: missing.append("opponent detail unavailable")
    if x.injury_data: score += 10
    else: missing.append("injury/on-off detail unavailable")
    if x.books_count >= 2 and x.over_odds is not None and x.under_odds is not None: score += 5
    else: missing.append("limited market consensus")
    if x.likely_defender_data: score += 3
    if x.tracking_data: score += 2
    return min(100, score), missing


def _clearance_metrics(values: list[float], line: float, side: str) -> dict:
    """Describe whether historical wins cleared tonight's line with room to spare.

    This is supporting, line-specific evidence. It never replaces the minutes/rate
    projection and is deliberately not used as a hard qualification gate.
    """
    recent = [float(v) for v in values[:10]]
    if not recent:
        return {"sample": 0, "hit_rate": None, "comfortable_rate": None,
                "barely_clear_rate": None, "median_margin": None,
                "median_winning_margin": None, "label": "UNAVAILABLE", "score": 50}
    margins = [(v - line) if side == "over" else (line - v) for v in recent]
    wins = [margin for margin in margins if margin > 0]
    comfort = max(1.0, abs(line) * .10)
    barely = max(.5, abs(line) * .05)
    comfortable_count = sum(margin >= comfort for margin in margins)
    barely_count = sum(0 < margin <= barely for margin in margins)
    hit_rate = len(wins) / len(margins)
    comfortable_rate = comfortable_count / len(margins)
    barely_clear_rate = barely_count / len(wins) if wins else 0.0
    median_margin = median(margins)
    median_winning_margin = median(wins) if wins else None

    # Hit frequency remains the larger piece, but repeated one-stat clears are
    # explicitly weaker than decisive clears. Score is used for comparison/rank.
    score = 100 * (.62 * hit_rate + .28 * comfortable_rate +
                   .10 * min(1.0, max(0.0, median_margin / comfort)))
    if hit_rate >= .60 and barely_clear_rate >= .60:
        label = "BARELY_SUPPORTED"
    elif comfortable_rate >= .60 and hit_rate >= .70:
        label = "DOMINANT"
    elif comfortable_rate >= .40 and hit_rate >= .60:
        label = "STRONG"
    elif hit_rate >= .50:
        label = "NORMAL"
    else:
        label = "WEAK"
    return {"sample": len(recent), "hit_rate": round(hit_rate, 4),
            "comfortable_rate": round(comfortable_rate, 4),
            "barely_clear_rate": round(barely_clear_rate, 4),
            "median_margin": round(median_margin, 2),
            "median_winning_margin": (round(median_winning_margin, 2)
                                      if median_winning_margin is not None else None),
            "comfort_threshold": round(comfort, 2), "label": label,
            "score": round(min(100, max(0, score)))}


def evaluate_prop(x: WNBAInput) -> WNBAEvaluation:
    hard, reasons, risks = [], [], list(x.warnings)
    status = x.availability.lower()
    if status in {"out", "inactive", "doubtful"}: hard.append(f"player status is {status}")
    if x.minutes_restriction: hard.append("confirmed minutes restriction")
    if x.projected_minutes < 12: hard.append("projected outside a viable prop rotation")
    if len(x.recent_values) < 5: hard.append("fewer than five usable games")
    if x.side not in {"over", "under"}: hard.append("invalid side")
    if x.line < 0: hard.append("invalid line")

    recent_rpm = x.recent_rate_per_minute
    if recent_rpm is None and x.recent_values and x.recent_minutes:
        pairs = [(v, m) for v, m in zip(x.recent_values, x.recent_minutes) if m >= 8]
        recent_rpm = sum(v for v, _ in pairs) / sum(m for _, m in pairs) if pairs else x.season_rate_per_minute
    blended_rpm = .72 * x.season_rate_per_minute + .28 * _shrink(
        recent_rpm or x.season_rate_per_minute, x.season_rate_per_minute,
        len(x.recent_values), 15,
    )

    context_factor = 1.0
    for factor in (x.opponent_factor, x.pace_factor, x.venue_factor, x.h2h_factor,
                   x.lineup_factor,
                   x.rest_factor, x.game_environment_factor,
                   x.usage_factor, x.efficiency_regression):
        context_factor *= min(1.18, max(.82, float(factor or 1)))
    context_factor = min(1.28, max(.72, context_factor))
    projected_mean = max(0, x.projected_minutes * blended_rpm * context_factor)

    minutes_sd = pstdev(x.recent_minutes[:10]) if len(x.recent_minutes) >= 2 else 4.5
    values_sd = pstdev(x.recent_values[:15]) if len(x.recent_values) >= 2 else max(1.5, projected_mean * .30)
    base_sd = max(1.0, values_sd * PROP_VARIANCE.get(x.prop_type, 1.0))
    role_volatility = min(.35, minutes_sd / max(12, x.projected_minutes))
    blowout = abs(x.spread or 0) >= 11
    standard_deviation = base_sd * (1 + role_volatility + (.10 if blowout else 0))
    if x.prop_type in {"steals", "blocks", "threes"}: risks.append("high-variance event stat")
    if minutes_sd >= 6: risks.append("volatile recent minutes")
    if blowout: risks.append("material blowout/minutes risk")
    if not x.role_confirmed: risks.append("role is not confirmed")

    # Continuity correction is intentionally modest for half-point sportsbook lines.
    raw_over_probability = 1 - _normal_cdf(x.line, projected_mean, standard_deviation)
    quality, missing = _quality(x)
    # Incomplete inputs shrink probabilities toward 50% instead of pretending
    # missing lineup/tracking information is neutral certainty.
    reliability = .65 + .35 * quality / 100
    over_probability = .5 + (raw_over_probability - .5) * reliability
    over_probability = min(.92, max(.08, over_probability))
    under_probability = 1 - over_probability
    selected = over_probability if x.side == "over" else under_probability
    market = no_vig_probability(x.over_odds, x.under_odds, x.side)
    if missing: risks.extend(missing)

    variance_score = round(min(100, 25 + standard_deviation / max(1, projected_mean) * 100
                               + role_volatility * 60 + (12 if blowout else 0)))
    variance_label = "LOW" if variance_score < 38 else "NORMAL" if variance_score < 58 else "HIGH" if variance_score < 78 else "EXTREME"
    minutes_confidence = round(max(0, min(100, 95 - minutes_sd * 5 - (15 if not x.role_confirmed else 0))))

    recent = x.recent_values[:10]
    clearance = _clearance_metrics(recent, x.line, x.side)
    clearance_adjustment = 0.0
    if recent:
        reasons.append(
            f"L{len(recent)} result rate {clearance['hit_rate']:.0%}; "
            f"comfortable clears {clearance['comfortable_rate']:.0%} ({clearance['label'].lower()})"
        )
        if clearance["label"] == "BARELY_SUPPORTED":
            risks.append("recent wins often barely cleared tonight's line")
            # A run of one-stat wins is less robust than the same hit rate with
            # room to spare. Keep the adjustment modest so trends never become
            # the model's primary driver.
            clearance_adjustment = -.015
        elif clearance["label"] in {"STRONG", "DOMINANT"}:
            reasons.append(f"median winning margin {clearance['median_winning_margin']:+g} vs tonight's line")
    venue_name = "home" if x.is_home is True else "road" if x.is_home is False else "venue"
    if x.venue_sample:
        reasons.append(f"{venue_name} split: {x.venue_sample} comparable games, sample-shrunk factor {x.venue_factor:.3f}")
    if x.h2h_sample:
        reasons.append(f"H2H: {x.h2h_sample} comparable games, low-weight factor {x.h2h_factor:.3f}")
    reasons.append(f"{x.projected_minutes:.1f} projected minutes at {blended_rpm:.3f} {x.prop_type}/minute")
    if abs(context_factor - 1) >= .02: reasons.append(f"combined role/matchup/environment factor {context_factor:.3f}")

    selected = min(.92, max(.08, selected + clearance_adjustment))
    if x.side == "over":
        over_probability, under_probability = selected, 1 - selected
    else:
        under_probability, over_probability = selected, 1 - selected
    edge = (selected - market) * 100 if market is not None else None

    publish = watchlist = False
    tier = "PASS"
    if not hard and market is not None:
        if selected >= .60 and edge >= 5 and quality >= 65 and variance_label != "EXTREME":
            tier, publish = "STRONG", True
        elif selected >= .56 and edge >= 2.5 and quality >= 55:
            tier, publish = "LEAN", True
        elif selected >= .53 and edge > 0 and quality >= 45:
            tier, watchlist = "WATCHLIST", True
    elif not hard and selected >= .56 and quality >= 45:
        tier, watchlist = "WATCHLIST", True
        risks.append("two-sided market price unavailable")

    priced_edge = min(15.0, max(-15.0, edge or 0))
    board_score = (selected * 100 + priced_edge * .65 + (quality - 60) * .10
                   - max(0, variance_score - 50) * .08
                   + (clearance["score"] - 50) * .08)
    board_score = min(100.0, max(0.0, board_score))

    return WNBAEvaluation(
        MODEL_VERSION, tier, publish, watchlist, round(projected_mean, 2),
        round(projected_mean, 2), round(max(0, projected_mean - 1.28 * standard_deviation), 2),
        round(projected_mean + 1.28 * standard_deviation, 2), round(standard_deviation, 2),
        round(over_probability, 4), round(under_probability, 4), round(selected, 4),
        round(market, 4) if market is not None else None, round(edge, 2) if edge is not None else None,
        probability_to_american(selected), quality, variance_score, variance_label,
        minutes_confidence, reasons, list(dict.fromkeys(risks)), hard,
        {"blended_rate_per_minute": round(blended_rpm, 4),
         "context_factor": round(context_factor, 4), "minutes_sd": round(minutes_sd, 2),
         "raw_over_probability": round(raw_over_probability, 4),
         "uncertainty_reliability": round(reliability, 4),
         "clearance": clearance,
         "clearance_probability_adjustment": clearance_adjustment,
         "venue_factor": round(x.venue_factor, 4), "venue_sample": x.venue_sample,
         "h2h_factor": round(x.h2h_factor, 4), "h2h_sample": x.h2h_sample,
         "board_score": round(board_score, 2),
         "recent_median": median(recent) if recent else None,
         "recent_average": round(mean(recent), 2) if recent else None},
    )
