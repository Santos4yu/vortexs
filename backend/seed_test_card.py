"""
Inserts one realistic test prop with a full stats_json payload so the
Discord embed can be previewed without needing a live +EV market.
Run once, then restart the bot and use /menu.
"""
import json, sqlite3, sys
from pathlib import Path

DB = Path(__file__).parent.parent / "vortex.db"

stats_json = json.dumps({
    "tier": "STRONG",
    "splits": {
        "season_avg": 1.93,
        "games_played": 142,
        "l5":  {"rate": 80.0, "hits": 4, "games": 5,  "avg": 2.4, "streak": 3},
        "l10": {"rate": 70.0, "hits": 7, "games": 10, "avg": 2.1, "streak": 3},
        "l20": {"rate": 65.0, "hits": 13,"games": 20, "avg": 1.9, "streak": 3},
        "recent_games": []
    },
    "pitcher": {
        "name": "Aaron Nola", "hand": "R",
        "era": "6.01", "fip": "4.41", "whip": "1.35",
        "k_per_9": "9.25", "bb_per_9": "2.67", "hr_per_9": "1.72",
        "avg_against": ".267",
        "last_5_starts": [
            {"date": "2025-09-26", "opponent": "Minnesota Twins",      "ip": "8.0", "k": 9, "er": 1},
            {"date": "2025-09-20", "opponent": "Arizona Diamondbacks", "ip": "5.1", "k": 4, "er": 4},
        ]
    },
    "bvp": {
        "ab": 39, "hits": 11, "avg": ".282", "hr": 1,
        "rbi": 5, "k": 10, "bb": 2, "tb": 18,
        "ops": "0.762", "sample": "large sample"
    },
    "platoon_note": "LHB vs RHP — standard platoon advantage for the batter.",
    "trend_signal": "HOT — elite recent form | Active 3-game hit streak"
})

conn = sqlite3.connect(DB)
cur  = conn.cursor()
cur.execute("DELETE FROM active_board WHERE player_name = 'Freddie Freeman'")
cur.execute("""
    INSERT INTO active_board
      (player_name, sport, stat_type, line, vortex_score, ev_percentage,
       case_summary, risk_summary, sportsbook, stats_json, tier)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
""", (
    "Freddie Freeman", "MLB", "Total Bases", 1.5, 74, 7.2,
    "Cross-book line discrepancy detected. FanDuel offering O1.5 TB at +124 against consensus fair price of +108.",
    "Strong edge at +7.2% EV across 2 books. Standard pricing. Line of 1.5 has solid sample depth. Watch for juice movement pre-game.",
    "FanDuel",
    stats_json, "STRONG"
))
conn.commit()
conn.close()
print("Test card inserted. Restart bot and run /menu.")
