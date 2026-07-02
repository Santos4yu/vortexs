import requests, json

yelich_id = 592885
nola_id   = 605400

# BvP
r = requests.get(f'https://statsapi.mlb.com/api/v1/people/{yelich_id}/stats', params={
    'stats': 'vsPlayer', 'group': 'hitting', 'opposingPlayerId': nola_id, 'sportId': 1
}, timeout=10)
print('BvP status:', r.status_code)
data = r.json()
splits = data.get('stats', [{}])[0].get('splits', [])
print(f'BvP splits: {len(splits)}')
if splits:
    print(json.dumps(splits[0]['stat'], indent=2))

# Pitcher hand
r2 = requests.get(f'https://statsapi.mlb.com/api/v1/people/{nola_id}', timeout=10)
p = r2.json()['people'][0]
print(f"\nNola pitchHand: {p.get('pitchHand', {}).get('code')} ({p.get('pitchHand', {}).get('description')})")

# Batter hand
r3 = requests.get(f'https://statsapi.mlb.com/api/v1/people/{yelich_id}', timeout=10)
y = r3.json()['people'][0]
print(f"Yelich batSide: {y.get('batSide', {}).get('code')} ({y.get('batSide', {}).get('description')})")

# Pitcher game log (recent starts)
r4 = requests.get(f'https://statsapi.mlb.com/api/v1/people/{nola_id}/stats', params={
    'stats': 'gameLog', 'group': 'pitching', 'season': 2025, 'sportId': 1
}, timeout=10)
d4 = r4.json()
psplits = d4.get('stats', [{}])[0].get('splits', [])
print(f'\nNola game log entries: {len(psplits)}')
if psplits:
    s = psplits[-1]['stat']
    print(f"Last start: IP={s.get('inningsPitched')} K={s.get('strikeOuts')} ER={s.get('earnedRuns')}")
    print(json.dumps(psplits[-1], indent=2)[:600])
