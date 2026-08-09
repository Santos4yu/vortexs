"""Minutes/opportunity-first WNBA player-prop probability model."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import mean, median, pstdev

MODEL_VERSION = "wnba-v1.0-foundation"

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
    role: str = "unknown"
    availability: str = "active"
    minutes_restriction: bool = False
    role_confirmed: bool = False
    opponent_factor: float = 1.0
    pace_factor: float = 1.0
    lineup_factor: float = 1.0
    rest_factor: float = 1.0
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
    for factor in (x.opponent_factor, x.pace_factor, x.lineup_factor,
                   x.rest_factor, x.usage_factor, x.efficiency_regression):
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
    over_probability = 1 - _normal_cdf(x.line, projected_mean, standard_deviation)
    over_probability = min(.92, max(.08, over_probability))
    under_probability = 1 - over_probability
    selected = over_probability if x.side == "over" else under_probability
    market = no_vig_probability(x.over_odds, x.under_odds, x.side)
    edge = (selected - market) * 100 if market is not None else None
    quality, missing = _quality(x)
    if missing: risks.extend(missing)

    variance_score = round(min(100, 25 + standard_deviation / max(1, projected_mean) * 100
                               + role_volatility * 60 + (12 if blowout else 0)))
    variance_label = "LOW" if variance_score < 38 else "NORMAL" if variance_score < 58 else "HIGH" if variance_score < 78 else "EXTREME"
    minutes_confidence = round(max(0, min(100, 95 - minutes_sd * 5 - (15 if not x.role_confirmed else 0))))

    recent = x.recent_values[:10]
    if recent:
        hit_rate = sum((v > x.line) if x.side == "over" else (v < x.line) for v in recent) / len(recent)
        reasons.append(f"L{len(recent)} result rate {hit_rate:.0%}; used as stability evidence only")
    reasons.append(f"{x.projected_minutes:.1f} projected minutes at {blended_rpm:.3f} {x.prop_type}/minute")
    if abs(context_factor - 1) >= .02: reasons.append(f"combined role/matchup/environment factor {context_factor:.3f}")

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
         "recent_median": median(recent) if recent else None,
         "recent_average": round(mean(recent), 2) if recent else None},
    )
