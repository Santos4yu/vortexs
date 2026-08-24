import unittest

from analyze import _matchup_score_100


def _factor(result, key):
    return next(item for item in result["factors"] if item["key"] == key)


class MatchupScoringRegressionTests(unittest.TestCase):
    def test_pete_factor_scale(self):
        result = _matchup_score_100(
            {"recent_batting_form": {"delta_pct": 5.0, "l10_ops": .985, "season_ops": .938}},
            pitcher={"hand": "R", "name": "Merrill Kelly", "era": 5.46,
                     "fip": 5.74, "whip": 1.54, "hr_per_9": 1.81},
            bvp={"ab": 5, "hits": 2, "avg": ".400"},
            vs_hand_splits={
                "R": {"avg": ".268", "ops": ".929", "pa": 395},
                "L": {"avg": ".306", "ops": ".957", "pa": 100},
            },
            prop_type="total_bases", weather={"dome": True},
        )
        self.assertEqual(_factor(result, "handedness")["impact"], -17)
        self.assertEqual(_factor(result, "pitcher_quality")["impact"], 19)
        self.assertEqual(_factor(result, "bvp")["impact"], 3)
        self.assertEqual(_factor(result, "recent_form")["impact"], 0)

    def test_kody_factor_scale_and_bullpen_is_context_only(self):
        result = _matchup_score_100(
            {"recent_batting_form": {"delta_pct": 19.7, "l10_ops": .831, "season_ops": .759}},
            pitcher={"hand": "L", "name": "Jeffrey Springs", "era": 6.20,
                     "fip": 6.09, "whip": 1.52, "hr_per_9": 2.30},
            bvp={"ab": 1, "hits": 1, "avg": "1.000"},
            vs_hand_splits={
                "L": {"avg": ".255", "ops": ".763", "pa": 114},
                "R": {"avg": ".232", "ops": ".758", "pa": 574},
            },
            opp_bullpen={"era": 6.0, "whip": 1.6, "total_ip": 20, "model_usable": True},
            prop_type="hits_runs_rbis",
        )
        self.assertEqual(_factor(result, "handedness")["impact"], 11)
        self.assertEqual(_factor(result, "recent_form")["impact"], 7)
        self.assertEqual(_factor(result, "bullpen")["impact"], 0)

    def test_arsenal_is_market_aware(self):
        arsenal = [{"pitch_type": "FF", "pitch_name": "Four-seam FB", "pct": 70}]
        rows = [{"pitch_type": "FF", "pa": 60, "avg": ".290",
                 "slg": ".330", "ops": ".620"}]
        hits = _matchup_score_100({}, arsenal=arsenal, bat_vs_pitch=rows, prop_type="hits")
        bases = _matchup_score_100({}, arsenal=arsenal, bat_vs_pitch=rows, prop_type="total_bases")
        self.assertGreater(_factor(hits, "arsenal_fit")["impact"], 0)
        self.assertLess(_factor(bases, "arsenal_fit")["impact"], 0)


if __name__ == "__main__":
    unittest.main()
