import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from wnba.model import WNBAInput, evaluate_prop, no_vig_probability
from wnba.service import _split_factor


def sample(**updates):
    values = dict(
        player_id="1", player_name="Test Player", team="AAA", opponent="BBB",
        game_date="2026-08-09", commence_time="2026-08-10T00:00:00Z",
        market_key="player_points", prop_type="points", side="over", line=20.5,
        over_odds=-110, under_odds=-110, best_book="draftkings",
        projected_minutes=34, season_minutes=32, recent_minutes=[34, 33, 35, 32, 34, 36, 33, 35, 34, 32],
        recent_values=[26, 24, 21, 23, 25, 19, 28, 22, 24, 20],
        season_rate_per_minute=.68, recent_rate_per_minute=.70, role="starter",
        role_confirmed=True, opponent_factor=1.04, pace_factor=1.03,
        books_count=3, injury_data=True, opponent_data=True, tracking_data=True,
    )
    values.update(updates)
    return WNBAInput(**values)


def test_balanced_market_devigs_to_half():
    assert no_vig_probability(-110, -110, "over") == .5


def test_good_edge_can_publish_without_perfect_optional_data():
    result = evaluate_prop(sample(likely_defender_data=False))
    assert result.tier in {"STRONG", "LEAN"}
    assert result.publish
    assert result.data_quality < 100


def test_hot_results_do_not_override_bad_opportunity():
    result = evaluate_prop(sample(
        projected_minutes=18, recent_values=[30] * 10,
        season_rate_per_minute=.45, recent_rate_per_minute=.45,
        opponent_factor=.90, pace_factor=.92,
    ))
    assert not result.publish


def test_confirmed_restriction_is_hard_rejection():
    result = evaluate_prop(sample(minutes_restriction=True))
    assert result.tier == "PASS"
    assert result.hard_rejections


def test_missing_two_sided_price_becomes_watchlist_not_fake_edge():
    result = evaluate_prop(sample(over_odds=-110, under_odds=None))
    assert result.edge_pp is None
    assert not result.publish


def test_barely_green_history_is_flagged_and_discounted():
    result = evaluate_prop(sample(
        line=20.5,
        recent_values=[21, 21, 21, 21, 21, 21, 19, 19, 19, 19],
    ))
    clearance = result.diagnostics["clearance"]
    assert clearance["hit_rate"] == .6
    assert clearance["label"] == "BARELY_SUPPORTED"
    assert result.diagnostics["clearance_probability_adjustment"] == -.015
    assert any("barely cleared" in risk for risk in result.risks)


def test_comfortable_clears_are_measured_separately_from_hit_rate():
    result = evaluate_prop(sample(
        line=20.5,
        recent_values=[27, 26, 25, 24, 24, 23, 19, 18, 17, 16],
    ))
    clearance = result.diagnostics["clearance"]
    assert clearance["hit_rate"] == .6
    assert clearance["comfortable_rate"] == .6
    assert clearance["label"] == "STRONG"
    assert clearance["median_winning_margin"] > 2


def test_venue_and_h2h_context_are_bounded_supporting_factors():
    neutral = evaluate_prop(sample(venue_factor=1.0, h2h_factor=1.0))
    favorable = evaluate_prop(sample(
        is_home=True, venue_factor=1.04, venue_sample=8,
        h2h_factor=1.02, h2h_sample=3,
    ))
    assert favorable.projected_mean > neutral.projected_mean
    assert favorable.diagnostics["venue_sample"] == 8
    assert any("home split" in reason for reason in favorable.reasons)
    assert any("H2H" in reason for reason in favorable.reasons)


def test_context_split_uses_matching_games_and_shrinks_small_samples():
    log = [
        {"minutes": 30, "points": 30, "is_home": True, "opponent": "BBB"},
        {"minutes": 30, "points": 24, "is_home": True, "opponent": "CCC"},
        {"minutes": 30, "points": 15, "is_home": False, "opponent": "BBB"},
        {"minutes": 30, "points": 15, "is_home": False, "opponent": "DDD"},
    ]
    factor, count = _split_factor(
        log, "points", lambda row: row["is_home"] is True,
        target=8, lower=.96, upper=1.04,
    )
    assert count == 2
    assert 1.0 < factor <= 1.04
