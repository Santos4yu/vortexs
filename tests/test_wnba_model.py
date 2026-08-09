import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from wnba.model import WNBAInput, evaluate_prop, no_vig_probability


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
