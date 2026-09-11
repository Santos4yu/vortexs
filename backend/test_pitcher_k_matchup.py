import unittest

from update_board import _pitcher_k_matchup_grade


BASE_SEASON = {
    "k_pct": 28.0, "k_per_9": 10.5, "batters_faced": 520,
    "games_started": 22, "innings_pitched": "132.0",
}
RECENT = [
    {"ip": "6.0", "k": 8}, {"ip": "5.2", "k": 7},
    {"ip": "6.1", "k": 9}, {"ip": "5.0", "k": 5},
    {"ip": "6.0", "k": 8},
]


class PitcherKMatchupGradeTests(unittest.TestCase):
    def grade(self, **overrides):
        args = dict(
            side="over", line=5.5, season_stats=BASE_SEASON,
            recent_starts=RECENT,
            opponent_overall={"k_pct": 25.0, "pa": 4200},
            opponent_vs_hand={"k_pct": 26.0, "pa": 1900},
            opponent_venue={"k_pct": 24.5, "pa": 2100},
            statcast_skill_score=72, arsenal_matchup_score=68,
        )
        args.update(overrides)
        return _pitcher_k_matchup_grade(**args)

    def test_grade_is_line_aware(self):
        self.assertGreater(self.grade(line=4.5)["score"], self.grade(line=7.5)["score"])

    def test_under_inverts_the_strikeout_environment(self):
        over = self.grade(side="over")["score"]
        under = self.grade(side="under")["score"]
        self.assertLess(under, over)

    def test_missing_optional_data_reduces_coverage_without_fake_neutral_values(self):
        full = self.grade()
        partial = self.grade(statcast_skill_score=None, arsenal_matchup_score=None)
        self.assertGreater(full["coverage"], partial["coverage"])
        self.assertTrue(all(factor["detail"] != "Unavailable" for factor in partial["factors"]))


if __name__ == "__main__":
    unittest.main()
