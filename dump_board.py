import sys, json
sys.path.insert(0, "backend")
import sqlite3

conn = sqlite3.connect("vortex.db")
rows = conn.execute(
    "SELECT player_name, sport, stat_type, line, vortex_score, ev_percentage, "
    "case_summary, risk_summary, sportsbook, stats_json, tier "
    "FROM active_board ORDER BY vortex_score DESC"
).fetchall()

for r in rows:
    sj = json.loads(r[9]) if r[9] else {}
    print(f"PLAYER={r[0]} | SPORT={r[1]} | STAT={r[2]} | LINE={r[3]} | SCR={r[4]} | EV={r[5]:.2f} | BOOK={r[8]} | TIER={r[10]} | SIDE={sj.get('side','over')}")
    splits = sj.get("splits") or {}
    if splits:
        l5  = splits.get("l5") or {}
        l10 = splits.get("l10") or {}
        l20 = splits.get("l20") or {}
        savg = splits.get("season_avg", "?")
        gp   = splits.get("games_played", "?")
        sig  = splits.get("trend_signal", sj.get("trend_signal", ""))
        print(f"  L5 : rate={l5.get('rate','?')}% hits={l5.get('hits','?')}/{l5.get('games','?')} avg={l5.get('avg','?')}")
        print(f"  L10: rate={l10.get('rate','?')}% hits={l10.get('hits','?')}/{l10.get('games','?')} avg={l10.get('avg','?')}")
        print(f"  L20: rate={l20.get('rate','?')}% hits={l20.get('hits','?')}/{l20.get('games','?')} avg={l20.get('avg','?')}")
        print(f"  Season avg={savg} GP={gp} | Signal={sig}")
    defense = sj.get("defense") or {}
    if defense:
        print(f"  DEF: team={defense.get('team_name')} avg_allowed={defense.get('avg_allowed')} rank={defense.get('league_rank')}")
    pitcher = sj.get("pitcher") or {}
    if pitcher:
        era = pitcher.get("era", "?")
        hand = pitcher.get("hand", "?")
        print(f"  PITCHER: hand={hand} ERA={era}")

conn.close()
