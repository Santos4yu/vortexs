"""Free ESPN WNBA collector. No MLB imports and no paid requests."""
from __future__ import annotations

import json, time, unicodedata
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
COMMON = "https://site.api.espn.com/apis/common/v3/sports/basketball/wnba"
CACHE = Path(__file__).resolve().parent / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VORTEX-WNBA/1.0"
IDX = {"min": 0, "points": 1, "rebounds": 2, "assists": 3, "steals": 4, "blocks": 5, "threes": 9}


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value.lower() if not unicodedata.combining(c) and c.isalnum())


def _num(value) -> float:
    try: return float(value)
    except (TypeError, ValueError): return 0.0


def _made(value) -> float:
    try: return float(str(value).split("-")[0])
    except (TypeError, ValueError): return 0.0


def _made_attempted(value) -> tuple[float, float]:
    try:
        made, attempted = str(value).split("-", 1)
        return float(made), float(attempted)
    except (TypeError, ValueError): return 0.0, 0.0


def _get(url: str, key: str, ttl_minutes: int) -> dict:
    path = CACHE / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl_minutes * 60:
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError): pass
    try:
        response = SESSION.get(url, timeout=15)
        response.raise_for_status()
        payload = response.json()
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload
    except (requests.RequestException, ValueError, OSError):
        if path.exists():
            try: return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError): pass
        return {}


def teams() -> list[dict]:
    data = _get(f"{SITE}/teams", "teams", 720)
    try:
        return [x["team"] for x in data["sports"][0]["leagues"][0]["teams"]]
    except (KeyError, IndexError, TypeError): return []


def roster_index() -> dict[str, dict]:
    index = {}
    for team in teams():
        tid, abbr = str(team.get("id")), team.get("abbreviation", "")
        roster = _get(f"{SITE}/teams/{tid}/roster", f"roster_{tid}", 360)
        for athlete in roster.get("athletes", []):
            name = athlete.get("displayName")
            if name:
                index[_norm(name)] = {"id": str(athlete.get("id")), "name": name,
                                      "team_id": tid, "team": abbr,
                                      "position": (athlete.get("position") or {}).get("abbreviation", "")}
    return index


def find_player(name: str, index: dict | None = None) -> dict | None:
    index, target = index or roster_index(), _norm(name)
    if target in index: return index[target]
    best = max(index, key=lambda k: SequenceMatcher(None, target, k).ratio(), default=None)
    return index[best] if best and SequenceMatcher(None, target, best).ratio() >= .84 else None


def schedule() -> list[dict]:
    now = datetime.now(timezone.utc)
    output, seen = [], set()
    for offset in (0, 1):
        day = (now + timedelta(days=offset)).strftime("%Y%m%d")
        data = _get(f"{SITE}/scoreboard?dates={day}", f"scoreboard_{day}", 10)
        for event in data.get("events", []):
            if event.get("id") in seen: continue
            try:
                comp = event["competitions"][0]
                sides = {c["homeAway"]: c["team"] for c in comp["competitors"]}
                output.append({"event_id": str(event["id"]), "commence_time": event["date"],
                               "state": event["status"]["type"]["state"],
                               "home": {"id": str(sides["home"]["id"]), "team": sides["home"]["abbreviation"]},
                               "away": {"id": str(sides["away"]["id"]), "team": sides["away"]["abbreviation"]}})
                seen.add(event["id"])
            except (KeyError, IndexError, TypeError): continue
    return output


def opponent_map(games: list[dict]) -> dict[str, dict]:
    out = {}
    for game in games:
        if game["state"] != "pre": continue
        out[game["home"]["team"]] = {"opponent": game["away"]["team"], "opponent_id": game["away"]["id"], "is_home": True, **game}
        out[game["away"]["team"]] = {"opponent": game["home"]["team"], "opponent_id": game["home"]["id"], "is_home": False, **game}
    return out


def game_log(player_id: str, fresh: bool = False) -> list[dict]:
    season = datetime.now(timezone.utc).year
    for year in (season, season - 1):
        data = _get(f"{COMMON}/athletes/{player_id}/gamelog?season={year}",
                    f"gamelog_{player_id}_{year}", 1 if fresh else 90)
        metadata, rows = data.get("events", {}), []
        for st in data.get("seasonTypes", []):
            for category in st.get("categories", []):
                for event in category.get("events", []):
                    stats, eid = event.get("stats", []), str(event.get("eventId", ""))
                    if len(stats) < 10 or not eid: continue
                    meta = metadata.get(eid, {}) or {}
                    fgm, fga = _made_attempted(stats[7]); tpm, tpa = _made_attempted(stats[9])
                    ftm, fta = _made_attempted(stats[11]) if len(stats) > 11 else (0.0, 0.0)
                    rows.append({"event_id": eid, "date": meta.get("gameDate") or meta.get("date") or "",
                                 "opponent": (meta.get("opponent") or {}).get("abbreviation", ""),
                                 "is_home": str(meta.get("atVs", "")).lower() == "vs",
                                 "minutes": _num(stats[0]), "points": _num(stats[1]),
                                 "rebounds": _num(stats[2]), "assists": _num(stats[3]),
                                 "steals": _num(stats[4]), "blocks": _num(stats[5]),
                                 "threes": tpm, "fgm": fgm, "fga": fga, "tpa": tpa,
                                 "ftm": ftm, "fta": fta})
        if rows:
            unique = {r["event_id"]: r for r in rows}
            return sorted(unique.values(), key=lambda r: r["date"], reverse=True)
    return []


def prop_values(log: list[dict], prop: str) -> list[float]:
    combos = {"pts_reb_ast": ("points", "rebounds", "assists"), "pts_reb": ("points", "rebounds"),
              "pts_ast": ("points", "assists"), "reb_ast": ("rebounds", "assists")}
    keys = combos.get(prop, (prop,))
    return [sum(float(row.get(k, 0)) for k in keys) for row in log]


def injuries() -> dict[str, list[dict]]:
    data = _get(f"{SITE}/injuries", "injuries", 20)
    output = {}
    for team in data.get("injuries", []):
        rows = []
        for item in team.get("injuries", []):
            athlete, status = item.get("athlete") or {}, str(item.get("status") or "").lower()
            name = athlete.get("displayName") or f"{athlete.get('firstName','')} {athlete.get('lastName','')}".strip()
            if name: rows.append({"name": name, "status": status})
        output[str(team.get("id"))] = rows
    return output


def player_status(team_id: str, name: str, report: dict | None = None) -> str:
    for row in (report or injuries()).get(str(team_id), []):
        if _norm(row["name"]) == _norm(name):
            s = row["status"]
            if s in {"out", "doubtful", "suspension", "injured reserve"}: return "out"
            if s in {"questionable", "day-to-day", "game-time decision"}: return "questionable"
            return "probable"
    return "active"


def team_profile(team_id: str) -> dict:
    data = _get(f"{SITE}/teams/{team_id}/statistics", f"team_{team_id}", 180)
    flat = {}
    try:
        for category in data["results"]["stats"]["categories"]:
            for stat in category.get("stats", []): flat[stat.get("name")] = stat.get("value")
    except (KeyError, TypeError): return {}
    try:
        fga = float(flat.get("avgFieldGoalsAttempted") or 0); oreb = float(flat.get("avgOffensiveRebounds") or 0)
        turnovers = float(flat.get("avgTurnovers") or 0); fta = float(flat.get("avgFreeThrowsAttempted") or 0)
        pace = fga - oreb + turnovers + .44 * fta
    except (TypeError, ValueError): pace = 0
    def first_number(*keys):
        for key in keys:
            value = flat.get(key)
            if value is not None:
                try: return float(value)
                except (TypeError, ValueError): pass
        return None
    return {"pace": round(pace, 2) if pace > 0 else None,
            "points": _num(flat.get("avgPoints")), "rebounds": _num(flat.get("avgRebounds")),
            "assists": _num(flat.get("avgAssists")), "threes": _num(flat.get("avgThreePointFieldGoalsMade")),
            "points_allowed": first_number("avgPointsAllowed", "avgOpponentPoints", "pointsAgainstPerGame"),
            "defensive_rating": first_number("defensiveRating", "defRating")}


def is_back_to_back(team_id: str, commence_time: str) -> bool:
    try: game_day = datetime.fromisoformat(commence_time.replace("Z", "+00:00")).date()
    except (ValueError, TypeError): return False
    schedule_data = _get(f"{SITE}/teams/{team_id}/schedule", f"team_schedule_{team_id}", 90)
    for event in schedule_data.get("events", []):
        try: prior = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).date()
        except (KeyError, ValueError, TypeError): continue
        if (game_day - prior).days == 1: return True
    return False


def lineup_status(event_id: str) -> dict[str, dict]:
    """Return ESPN-confirmed participants/starters when the pregame box is posted."""
    payload = _get(f"{SITE}/summary?event={event_id}", f"lineup_{event_id}", 5)
    output = {}
    for team in (payload.get("boxscore") or {}).get("players", []):
        for group in team.get("statistics", []):
            for row in group.get("athletes", []):
                athlete = row.get("athlete") or {}; pid = str(athlete.get("id") or "")
                if pid:
                    output[pid] = {"active": bool(row.get("active", True)),
                                   "starter": bool(row.get("starter", False))}
    return output


def game_state(event_id: str) -> str:
    payload = _get(f"{SITE}/summary?event={event_id}", f"state_{event_id}", 2)
    try: return payload["header"]["competitions"][0]["status"]["type"]["state"]
    except (KeyError, IndexError, TypeError): return ""


def team_event_on_date(team_id: str, game_date: str) -> str | None:
    payload = _get(f"{SITE}/teams/{team_id}/schedule", f"team_schedule_{team_id}", 30)
    for event in payload.get("events", []):
        raw = str(event.get("date") or "")
        try:
            local = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone(timedelta(hours=-7))).date().isoformat()
        except (ValueError, TypeError): local = raw[:10]
        if local == game_date: return str(event.get("id"))
    return None
