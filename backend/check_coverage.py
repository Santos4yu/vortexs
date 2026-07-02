import json
from pathlib import Path
from collections import defaultdict

print("\n=== Book coverage per market ===\n")
for f in sorted(Path(__file__).parent / "cache").glob("*.json") if False else sorted((Path(__file__).parent / "cache").glob("*.json")):
    data = json.loads(f.read_text(encoding="utf-8"))
    sport_market = f.stem
    prop_books: dict[str, set] = defaultdict(set)
    for event in data:
        for bm in event.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    if o.get("name", "").lower() == "over":
                        desc = o.get("description") or ""
                        pt   = o.get("point", "")
                        key  = f"{desc}|{pt}"
                        prop_books[key].add(bm["key"])
    multi = sum(1 for b in prop_books.values() if len(b) >= 2)
    total = len(prop_books)
    print(f"  {sport_market:45}  total={total:3}  2+books={multi}")
