"""Fetch PrizePicks projections — used by /goblins for actual goblin lines."""

import json
import time
from pathlib import Path

CACHE_DIR     = Path(__file__).parent / "cache"
CACHE_FILE    = CACHE_DIR / "prizepicks_goblins.json"
CACHE_TTL_SEC = 45

WORKER_BASE = "https://mlb-proxy.damian209466-d45.workers.dev"
API_URL = f"{WORKER_BASE}/prizepicks/projections"

try:
    from curl_cffi import requests as _req
    _SESSION = _req.Session()
except ImportError:
    try:
        import cloudscraper
        _SESSION = cloudscraper.create_scraper()
    except ImportError:
        import requests as _req
        _SESSION = _req.Session()


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_SEC


def _load_cache():
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(data):
    CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def fetch_goblins():
    """Return all active MLB goblin projections from PrizePicks.

    Returns a list of dicts with keys:
        player_name, team, position, stat_type, line_score,
        projection_type, description, ppid
    """
    if _is_fresh(CACHE_FILE):
        return _load_cache()

    try:
        resp = _SESSION.get(API_URL, timeout=30)
        if resp.status_code == 403:
            snippet = resp.text[:500]
            raise RuntimeError(
                f"PrizePicks 403 Forbidden — body: {snippet}"
            )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        if CACHE_FILE.exists():
            return _load_cache()
        raise RuntimeError(f"PrizePicks API unavailable: {exc}") from exc

    players = {}
    for inc in payload.get("included", []):
        if inc.get("type") == "new_player":
            a = inc.get("attributes", {})
            players[str(inc["id"])] = {
                "name": a.get("display_name", "?"),
                "team": a.get("team_name", ""),
                "position": a.get("position", ""),
            }

    goblins = []
    for p in payload.get("data", []):
        a = p.get("attributes", {})
        if a.get("odds_type") != "goblin":
            continue

        rels = p.get("relationships", {})
        league = rels.get("league", {}).get("data", {})
        if league.get("id") != "2":
            continue

        pid = str(rels.get("new_player", {}).get("data", {}).get("id", ""))
        pl = players.get(pid, {"name": "?", "team": "", "position": ""})

        goblins.append({
            "player_name": pl["name"],
            "team": pl["team"],
            "position": pl["position"],
            "stat_type": a.get("stat_type", "?"),
            "line_score": a.get("line_score", "?"),
            "projection_type": a.get("projection_type", "Single Stat"),
            "description": a.get("description", ""),
            "ppid": pid,
        })

    _save_cache(goblins)
    return goblins
