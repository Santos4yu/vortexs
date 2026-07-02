import sys, json, traceback
sys.path.insert(0, 'backend')
from pathlib import Path
from update_board import (parse_events, enrich_mlb, MARKET_TO_PROP_TYPE,
                          stats_mlb)

# Load the HRR cache
cache = Path('backend/cache/baseball_mlb__batter_hits_runs_rbis.json')
if not cache.exists():
    print("No HRR cache — run engine first")
    sys.exit(1)

data = json.loads(cache.read_text())
rows = parse_events(data, 'MLB', 'batter_hits_runs_rbis')
print(f"Parsed: {len(rows)} rows")
print(f"Sample: {rows[0]['player_name']} O{rows[0]['line']} ev={rows[0]['ev_percentage']:.1f}%")

# Build a minimal pitcher_lookup
schedule = stats_mlb.get_todays_schedule()
pitcher_lookup = {}
for gid, info in schedule.items():
    if info.get("home_pitcher"):
        pitcher_lookup[info["home_team_id"]] = info["home_pitcher"]
    if info.get("away_pitcher"):
        pitcher_lookup[info["away_team_id"]] = info["away_pitcher"]

print(f"Pitcher lookup: {len(pitcher_lookup)} teams")

# Try enriching just the first 3 rows
try:
    result = enrich_mlb(rows[:3], pitcher_lookup)
    print(f"Enriched {len(result)} rows OK")
    for r in result:
        print(f"  {r['player_name']} ev={r['ev_percentage']:.1f}% tier={r['tier']}")
except Exception as e:
    traceback.print_exc()
