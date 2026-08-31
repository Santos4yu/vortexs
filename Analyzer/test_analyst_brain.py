import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyst_brain
import pitcher_brain
import analyzer


class AnalystBrainTests(unittest.TestCase):
    def test_every_market_has_100_weight_points(self):
        for market, weights in analyst_brain.MARKET_WEIGHTS.items():
            self.assertEqual(sum(weights.values()), 100, market)

    def test_missing_data_shrinks_toward_neutral(self):
        result = analyst_brain.evaluate(
            prop="hits", side="over", line=.5,
            splits={}, matchup_factors=[], statcast={}, lineup_spot=None,
            team_profile={}, bullpen={}, park={}, weather={}, bvp={},
            pitch_profile=[], arsenal=[],
            home_away={}, batter_is_home=None, pitcher_venue={}, pitcher_season_era=None,
        )
        self.assertGreaterEqual(result["score"], 45)
        self.assertLessEqual(result["score"], 55)
        self.assertLess(result["coverage"], .25)

    def test_over_and_under_baselines_invert(self):
        common = dict(
            prop="hits", line=.5,
            splits={"l5": {"rate": 80}, "l10": {"rate": 70},
                    "l20": {"rate": 65}, "season_avg": .8},
            matchup_factors=[], statcast={}, lineup_spot=None,
            team_profile={}, bullpen={}, park={}, weather={}, bvp={},
            pitch_profile=[], arsenal=[],
            home_away={}, batter_is_home=None, pitcher_venue={}, pitcher_season_era=None,
        )
        over = analyst_brain.evaluate(side="over", **common)
        under = analyst_brain.evaluate(side="under", **common)
        self.assertGreater(over["score"], 50)
        self.assertLess(under["score"], 50)

    def test_pitcher_k_weights_total_100(self):
        self.assertEqual(sum(pitcher_brain.WEIGHTS.values()), 100)

    def test_pitcher_k_missing_data_stays_neutral(self):
        result = pitcher_brain.evaluate(
            side="over", line=5.5, card={"splits": {}}, pitcher_metrics={},
            pitcher_pitches=[], lineup_pitch_profile={}, lineup_confirmed=False,
            opponent_profile={}, park={}, weather={}, team_history={},
            pitcher_is_home=None, lineup_bvp={},
        )
        self.assertGreaterEqual(result["score"], 45)
        self.assertLessEqual(result["score"], 55)

    def test_one_strong_pitcher_signal_cannot_create_a_play(self):
        result = pitcher_brain.evaluate(
            side="over", line=5.5,
            card={"splits": {"l5": {"rate": 100}, "l10": {"rate": 100},
                             "l20": {"rate": 100}, "season_avg": 10}},
            pitcher_metrics={}, pitcher_pitches=[], lineup_pitch_profile={},
            lineup_confirmed=False,
            opponent_profile={"avg": .250, "wrc_plus": 100, "bb_pct": 8.5},
            park={}, weather={}, team_history={}, pitcher_is_home=None, lineup_bvp={},
        )
        self.assertEqual(result["consensus"]["supporting"], 1)
        self.assertLessEqual(result["score"], 59)

    def test_name_matching_is_accent_insensitive(self):
        self.assertGreater(analyzer._name_score("randy vazquez", "Randy Vásquez"), .85)
        self.assertLess(analyzer._name_score("randy vazquez", "Daniel Vazquez"), .80)


    @patch("analyzer.stats_mlb._get")
    def test_pitcher_team_history_aggregates_all_valid_rows(self, get):
        get.return_value = {"stats": [{"splits": [
            {"stat": {"inningsPitched": "5.2", "strikeOuts": 6, "earnedRuns": 2}},
            {"stat": {"inningsPitched": "6.1", "strikeOuts": 7, "earnedRuns": 1}},
        ]}]}
        history = analyzer._pitcher_vs_team_history(
            1, 2, "Toronto Blue Jays",
            [{"opponent": "Toronto Blue Jays", "value": 7}],
        )
        self.assertEqual(history["career_ip"], 12.0)
        self.assertEqual(history["career_k"], 13)
        self.assertTrue(history["career_valid"])
        self.assertEqual(history["current_values"], [7.0])

    @patch("analyzer.stats_mlb._get")
    def test_incomplete_pitcher_team_history_is_withheld(self, get):
        get.return_value = {"stats": [{"splits": [
            {"stat": {"strikeOuts": 2}},
        ]}]}
        history = analyzer._pitcher_vs_team_history(1, 2, "Toronto Blue Jays", [])
        self.assertFalse(history["career_valid"])
        self.assertEqual(history["career_k"], 0)

    @patch("analyzer.stats_mlb.get_game_weather")
    @patch("analyzer.context_data.get_park_factor")
    @patch("analyzer.stats_mlb.get_todays_schedule")
    def test_environment_never_uses_an_unrelated_game(self, schedule, park, weather):
        schedule.return_value = {99: {
            "gamePk": 99, "home_team_id": 119, "away_team_id": 137,
            "home_team_name": "Los Angeles Dodgers", "home_abbr": "LAD",
            "game_utc": "2026-08-10T02:10:00Z",
        }}
        game, park_data, weather_data = analyzer._game_environment({
            "game_pk": 55, "game_utc": "2026-08-10T23:10:00Z",
            "is_home": True, "home_team_id": 116, "opp_team_id": 118,
        })
        self.assertEqual(game, {})
        self.assertEqual(park_data, {})
        self.assertEqual(weather_data, {})
        park.assert_not_called()
        weather.assert_not_called()

    def test_compact_report_hides_internal_weight_dump(self):
        result = {
            "analysis_type": "pitcher_strikeouts",
            "bet": {"player": "Test Pitcher", "side": "over", "line": 5.5},
            "confidence": {"score": 58, "rating": "NEUTRAL"},
            "matchup": {"player_team": "Home", "opponent": "Away", "is_home": True},
            "analyst_evidence": [{"key": "baseline", "score": 60, "weight": 25,
                                  "reliability": 1, "detail": "Recent results support the play"}],
            "analyst_consensus": {"supporting": 1, "opposing": 0, "neutral": 0},
            "analyst_coverage": .8, "card": {"splits": {}, "season_stats": {}},
            "opponent_profile": {}, "lineup": [], "game": {},
        }
        report = analyzer.format_report(result)
        self.assertIn("DECISION: PASS", report)
        self.assertNotIn("weight 25", report)
        self.assertNotIn("reliability 100%", report)


if __name__ == "__main__":
    unittest.main()
