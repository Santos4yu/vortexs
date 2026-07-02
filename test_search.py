import sys
sys.path.insert(0, 'backend')
import requests

BASE = "https://statsapi.mlb.com/api/v1"

def _api(name):
    try:
        r = requests.get(f"{BASE}/people/search", params={"names": name, "sportId": 1}, timeout=10)
        r.raise_for_status()
        return r.json().get("people", [])
    except Exception as e:
        print(f"  API error: {e}")
        return []

queries = ["Alex Call", "Chandler Simpson", "alex call", "chandler simpson", "Call, Alex", "Simpson, Chandler"]
for q in queries:
    res = _api(q)
    hits = [(p["fullName"], p.get("active"), p.get("currentTeam", {}).get("name", "?")) for p in res[:3]]
    print(f"Query: {repr(q)} -> {hits}")
