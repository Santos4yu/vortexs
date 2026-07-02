import json
from pathlib import Path

cache = Path(__file__).parent / "cache"
for f in sorted(cache.glob("*.json")):
    data = json.loads(f.read_text(encoding="utf-8"))
    print(f"\n=== {f.name} ===")
    if not data:
        print("  EMPTY")
        continue
    for event in data[:2]:
        bms = event.get("bookmakers", [])
        print(f"  books: {[b['key'] for b in bms]}")
        for bm in bms[:3]:
            for mkt in bm.get("markets", []):
                outcomes = mkt.get("outcomes", [])[:3]
                for o in outcomes:
                    print(f"    {bm['key']:15} | {o.get('description','?'):20} | {o.get('name','?'):6} | line={o.get('point')} | price={o.get('price')}")
