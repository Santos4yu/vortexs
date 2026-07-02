import sys, json
sys.path.insert(0, 'backend')
from pathlib import Path
from update_board import parse_events, consensus_no_vig_prob, american_to_implied

cache = Path('backend/cache/baseball_mlb__batter_hits_runs_rbis.json')
data = json.loads(cache.read_text())
rows = parse_events(data, 'MLB', 'batter_hits_runs_rbis')
print(f'Total rows parsed: {len(rows)}')
above = [r for r in rows if r['ev_percentage'] >= 1.5]
print(f'Above EV floor: {len(above)}')
for r in rows[:8]:
    print(f"  {r['player_name']} O{r['line']} side={r['side']} ev={r['ev_percentage']:.1f}% book={r['best_book']} n={r['n_books']}")

# Also check raw book data for one game
game = data[0]
print('\nBooks in first game:')
for bm in game.get('bookmakers', []):
    for mkt in bm.get('markets', []):
        o_prices = [o['price'] for o in mkt['outcomes'] if o['name'].lower()=='over']
        u_prices = [o['price'] for o in mkt['outcomes'] if o['name'].lower()=='under']
        print(f"  {bm['key']}: O={o_prices[:2]} U={u_prices[:2]}")
