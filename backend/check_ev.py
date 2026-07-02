"""Show EV distribution for multi-book props to calibrate the threshold."""
import json, math
from pathlib import Path
from collections import defaultdict

def american_to_decimal(o): return (o/100)+1 if o>0 else (100/abs(o))+1
def american_to_implied(o): return 100/(o+100) if o>0 else abs(o)/(abs(o)+100)
def no_vig(ov, un):
    p = american_to_implied(ov); q = american_to_implied(un)
    return p / (p + q)
def ev(prob, best):
    return round(((prob * american_to_decimal(best)) - 1) * 100, 2)

for f in sorted((Path(__file__).parent / "cache").glob("*.json")):
    data = json.loads(f.read_text(encoding="utf-8"))
    prop_map = defaultdict(lambda: defaultdict(lambda: {"over": {}, "under": {}}))
    for event in data:
        for bm in event.get("bookmakers", []):
            book = bm["key"]
            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    side = o.get("name","").lower()
                    desc = o.get("description") or o.get("name","")
                    pt, price = o.get("point"), o.get("price")
                    if side in ("over","under") and pt is not None and price is not None:
                        prop_map[desc][float(pt)][side][book] = int(price)

    evs = []
    for player, lines in prop_map.items():
        for line, sides in lines.items():
            om, um = sides["over"], sides["under"]
            if len(om) < 2: continue
            probs = [no_vig(om[b], um[b]) for b in om if b in um]
            if not probs: continue
            true_p = sum(probs)/len(probs)
            best = max(om, key=lambda b: american_to_decimal(om[b]))
            if om[best] < -200: continue
            evs.append(ev(true_p, om[best]))

    if evs:
        evs.sort(reverse=True)
        pos = sum(1 for e in evs if e >= 1.5)
        print(f"\n{f.stem}")
        print(f"  multi-book props: {len(evs)}  |  >=+1.5% EV: {pos}")
        print(f"  top EVs: {evs[:8]}")
        print(f"  EV range: {evs[-1]:.1f}% to {evs[0]:.1f}%")
