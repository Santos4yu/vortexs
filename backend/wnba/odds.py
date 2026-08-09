"""Paid WNBA market collector. This is the only WNBA module spending credits."""
from __future__ import annotations

import json, os, time, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

SPORT_KEY = "basketball_wnba"
BASE = "https://api.the-odds-api.com/v4"
MARKETS = {
    "player_points": "points", "player_rebounds": "rebounds",
    "player_assists": "assists", "player_threes": "threes",
    "player_points_rebounds_assists": "pts_reb_ast",
    "player_points_rebounds": "pts_reb", "player_points_assists": "pts_ast",
    "player_rebounds_assists": "reb_ast",
}
BOOKS = ("draftkings", "fanduel", "betmgm", "espnbet", "betrivers", "pinnacle")
CACHE = Path(__file__).resolve().parent / "cache" / "odds.json"
DB = Path(__file__).resolve().parents[2] / "vortex.db"
SESSION = requests.Session()


def _load_cache(max_age_minutes: int = 90) -> list[dict]:
    if not CACHE.exists() or time.time() - CACHE.stat().st_mtime > max_age_minutes * 60: return []
    try: return json.loads(CACHE.read_text(encoding="utf-8"))
    except (ValueError, OSError): return []


def fetch(force: bool = False) -> tuple[list[dict], dict]:
    if not force:
        cached = _load_cache(60)
        if cached: return cached, {"cached": True, "credits": 0}
    key = os.getenv("ODDS_API_KEY", "")
    if not key: return _load_cache(10080), {"error": "ODDS_API_KEY missing", "credits": 0}
    try:
        events_response = SESSION.get(f"{BASE}/sports/{SPORT_KEY}/events",
                                      params={"apiKey": key, "dateFormat": "iso"}, timeout=15)
        events_response.raise_for_status()
        events = events_response.json()
    except (requests.RequestException, ValueError) as exc:
        return _load_cache(10080), {"error": str(exc), "credits": 0}
    now, rows, credits = datetime.now(timezone.utc), [], 0
    eligible = []
    for event in events:
        try: start = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError): continue
        if now < start <= now + timedelta(days=2): eligible.append(event)
    estimate = len(eligible) * (len(MARKETS) + 2)
    reserve = int(os.getenv("ODDS_MONTHLY_CREDIT_RESERVE", "15000"))
    try: remaining = int(events_response.headers.get("x-requests-remaining"))
    except (TypeError, ValueError): remaining = None
    if remaining is not None and remaining - estimate < reserve:
        return _load_cache(10080), {"error": "monthly odds reserve protected", "credits": 0, "remaining": remaining}
    budget_day, daily_cap = now.date().isoformat(), int(os.getenv("WNBA_DAILY_CREDIT_CAP", "700"))
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS wnba_odds_budget (budget_day TEXT PRIMARY KEY,credits_used INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT OR IGNORE INTO wnba_odds_budget VALUES (?,0)", (budget_day,))
    used = conn.execute("SELECT credits_used FROM wnba_odds_budget WHERE budget_day=?", (budget_day,)).fetchone()[0]
    conn.close()
    if used + estimate > daily_cap:
        return _load_cache(10080), {"error": f"WNBA daily odds cap protected ({used}/{daily_cap})", "credits": 0}
    requested = ",".join([*MARKETS, "spreads", "totals"])
    for event in eligible:
        try:
            response = SESSION.get(f"{BASE}/sports/{SPORT_KEY}/events/{event['id']}/odds",
                params={"apiKey": key, "regions": "us", "markets": requested,
                        "oddsFormat": "american", "bookmakers": ",".join(BOOKS)}, timeout=20)
            response.raise_for_status(); payload = response.json()
            credits += int(response.headers.get("x-requests-last", 0) or 0)
            rows.append(payload)
        except (requests.RequestException, ValueError): continue
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(rows), encoding="utf-8")
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR IGNORE INTO wnba_odds_budget VALUES (?,0)", (budget_day,))
    conn.execute("UPDATE wnba_odds_budget SET credits_used=credits_used+? WHERE budget_day=?", (credits, budget_day))
    conn.execute("DELETE FROM wnba_odds_budget WHERE budget_day < ?", (budget_day,)); conn.commit(); conn.close()
    return rows, {"cached": False, "credits": credits,
                  "remaining": response.headers.get("x-requests-remaining") if rows else None}


def parse(events: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    props, games = {}, {}
    for event in events:
        eid = str(event.get("id", "")); game = games.setdefault(eid, {
            "event_id": eid, "commence_time": event.get("commence_time", ""),
            "home_team": event.get("home_team", ""), "away_team": event.get("away_team", ""),
            "spread": None, "total": None})
        for bookmaker in event.get("bookmakers", []):
            book = bookmaker.get("key", "")
            for market in bookmaker.get("markets", []):
                key = market.get("key")
                if key == "totals":
                    over = next((o for o in market.get("outcomes", []) if o.get("name") == "Over"), None)
                    if over and game["total"] is None: game["total"] = over.get("point")
                    continue
                if key == "spreads":
                    home = next((o for o in market.get("outcomes", []) if o.get("name") == game["home_team"]), None)
                    if home and game["spread"] is None: game["spread"] = home.get("point")
                    continue
                prop_type = MARKETS.get(key)
                if not prop_type: continue
                for outcome in market.get("outcomes", []):
                    side = str(outcome.get("name", "")).lower()
                    player, line, price = outcome.get("description"), outcome.get("point"), outcome.get("price")
                    if not player or side not in {"over", "under"} or line is None or price is None: continue
                    natural = (eid, player.casefold(), key, float(line))
                    row = props.setdefault(natural, {**game, "player_name": player, "market_key": key,
                                                     "prop_type": prop_type, "line": float(line),
                                                     "over": {}, "under": {}})
                    row[side][book] = int(price)
    for row in props.values():
        context = games.get(row["event_id"], {})
        row["spread"], row["total"] = context.get("spread"), context.get("total")
    return list(props.values()), games


def best_prices(row: dict) -> tuple[int | None, int | None, str]:
    """Return a same-book pair for no-vig math; never cross-pair prices."""
    common = set(row["over"]) & set(row["under"])
    if not common: return None, None, ""
    if "pinnacle" in common: book = "pinnacle"
    else:
        # Choose the lowest-overround paired book as the cleanest market anchor.
        def overround(key):
            def implied(price): return abs(price) / (abs(price) + 100) if price < 0 else 100 / (price + 100)
            return implied(row["over"][key]) + implied(row["under"][key])
        book = min(common, key=overround)
    return int(row["over"][book]), int(row["under"][book]), book
