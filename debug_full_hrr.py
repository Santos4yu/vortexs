"""
Debug: run enrich_mlb on ALL HRR rows (not just 3) to find where the full-run crash is.
"""
import sys, json, traceback
sys.path.insert(0, 'backend')
from pathlib import Path

# Patch stdout to flush immediately
import builtins
_orig_print = builtins.print
def _print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _orig_print(*args, **kwargs)
builtins.print = _print

from update_board import parse_events, enrich_mlb, stats_mlb

cache = Path('backend/cache/baseball_mlb__batter_hits_runs_rbis.json')
if not cache.exists():
    print("No HRR cache — run engine first"); sys.exit(1)

data = json.loads(cache.read_text())
rows = parse_events(data, 'MLB', 'batter_hits_runs_rbis')
print(f"Parsed: {len(rows)} rows")

schedule = stats_mlb.get_todays_schedule()
pitcher_lookup = {}
for gid, info in schedule.items():
    if info.get("home_pitcher"):
        pitcher_lookup[info["home_team_id"]] = info["home_pitcher"]
    if info.get("away_pitcher"):
        pitcher_lookup[info["away_team_id"]] = info["away_pitcher"]
print(f"Pitcher lookup: {len(pitcher_lookup)} teams")

# Wrap enrich_mlb row-by-row to find the crash
import update_board as ub

enriched = []
for i, row in enumerate(rows):
    try:
        result = ub.enrich_mlb([row], pitcher_lookup)
        enriched.extend(result)
    except Exception as e:
        print(f"\n=== CRASH on row {i}: {row.get('player_name')} {row.get('side')} {row.get('line')} ===")
        traceback.print_exc()
        print("Row data:", json.dumps({k: v for k, v in row.items() if k not in ('over_map', 'under_map')}, default=str))
        break

print(f"\nDone: {len(enriched)} enriched rows processed before crash (if any)")
