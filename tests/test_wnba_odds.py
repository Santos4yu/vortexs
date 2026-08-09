import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from wnba.odds import best_prices, parse


def event_payload():
    def market(book, over, under):
        return {"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Test Player", "point": 20.5, "price": over},
            {"name": "Under", "description": "Test Player", "point": 20.5, "price": under},
        ]}
    return [{"id": "game-1", "commence_time": "2099-01-01T00:00:00Z",
             "home_team": "Home", "away_team": "Away", "bookmakers": [
                 {"key": "draftkings", "markets": [market("draftkings", -105, -120)]},
                 {"key": "pinnacle", "markets": [market("pinnacle", -110, -110)]},
             ]}]


def test_player_market_parsing_pairs_sides():
    rows, _ = parse(event_payload())
    assert len(rows) == 1
    assert rows[0]["over"]["draftkings"] == -105
    assert rows[0]["under"]["pinnacle"] == -110


def test_no_vig_anchor_uses_same_book_and_prefers_pinnacle():
    row = parse(event_payload())[0][0]
    over, under, book = best_prices(row)
    assert (over, under, book) == (-110, -110, "pinnacle")
