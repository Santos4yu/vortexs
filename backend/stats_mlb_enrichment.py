"""
Vortex — MLB Enrichment Engine
================================
Adds park factors, weather, pitch-type BvP, team OAA, platoon splits,
and team K rankings to the base MLB card. All sources are free.

Sources:
  - wttr.in           : weather (no key)
  - Baseball Savant   : pitch-type splits, OAA, statcast (no key)
  - MLB Stats API     : platoon splits, team K rankings (no key)
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

import stats_mlb
BASE = stats_mlb.BASE

log = logging.getLogger("vortex.mlb_enrichment")

CACHE_DIR = Path(__file__).parent / "cache" / "mlb_enrich"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = 12
DELAY   = 0.3

# ── Park factors (2024-25 multi-year averages, runs-based) ────────────────────
# >1.0 = hitter friendly, <1.0 = pitcher friendly
PARK_FACTORS: dict[str, dict] = {
    "Colorado Rockies":      {"factor": 1.28, "name": "Coors Field",         "city": "Denver"},
    "Boston Red Sox":        {"factor": 1.10, "name": "Fenway Park",          "city": "Boston"},
    "Cincinnati Reds":       {"factor": 1.09, "name": "Great American",       "city": "Cincinnati"},
    "Philadelphia Phillies": {"factor": 1.07, "name": "Citizens Bank Park",   "city": "Philadelphia"},
    "Texas Rangers":         {"factor": 1.06, "name": "Globe Life Field",      "city": "Arlington"},
    "Kansas City Royals":    {"factor": 1.05, "name": "Kauffman Stadium",     "city": "Kansas City"},
    "Chicago Cubs":          {"factor": 1.05, "name": "Wrigley Field",         "city": "Chicago"},
    "Atlanta Braves":        {"factor": 1.04, "name": "Truist Park",           "city": "Atlanta"},
    "Baltimore Orioles":     {"factor": 1.04, "name": "Camden Yards",          "city": "Baltimore"},
    "Arizona Diamondbacks":  {"factor": 1.03, "name": "Chase Field",           "city": "Phoenix"},
    "Milwaukee Brewers":     {"factor": 1.02, "name": "American Family Field", "city": "Milwaukee"},
    "Toronto Blue Jays":     {"factor": 1.02, "name": "Rogers Centre",         "city": "Toronto"},
    "St. Louis Cardinals":   {"factor": 1.01, "name": "Busch Stadium",         "city": "St. Louis"},
    "New York Mets":         {"factor": 1.01, "name": "Citi Field",            "city": "New York"},
    "Washington Nationals":  {"factor": 1.00, "name": "Nationals Park",        "city": "Washington"},
    "Minnesota Twins":       {"factor": 1.00, "name": "Target Field",          "city": "Minneapolis"},
    "Detroit Tigers":        {"factor": 0.99, "name": "Comerica Park",         "city": "Detroit"},
    "Los Angeles Angels":    {"factor": 0.99, "name": "Angel Stadium",         "city": "Anaheim"},
    "Chicago White Sox":     {"factor": 0.99, "name": "Guaranteed Rate Field", "city": "Chicago"},
    "Tampa Bay Rays":        {"factor": 0.98, "name": "Tropicana Field",       "city": "St. Petersburg"},
    "Seattle Mariners":      {"factor": 0.97, "name": "T-Mobile Park",         "city": "Seattle"},
    "Houston Astros":        {"factor": 0.97, "name": "Minute Maid Park",      "city": "Houston"},
    "New York Yankees":      {"factor": 0.97, "name": "Yankee Stadium",        "city": "New York"},
    "Cleveland Guardians":   {"factor": 0.97, "name": "Progressive Field",     "city": "Cleveland"},
    "Pittsburgh Pirates":    {"factor": 0.96, "name": "PNC Park",              "city": "Pittsburgh"},
    "Los Angeles Dodgers":   {"factor": 0.96, "name": "Dodger Stadium",        "city": "Los Angeles"},
    "Miami Marlins":         {"factor": 0.96, "name": "loanDepot park",        "city": "Miami"},
    "San Diego Padres":      {"factor": 0.95, "name": "Petco Park",            "city": "San Diego"},
    "San Francisco Giants":  {"factor": 0.95, "name": "Oracle Park",           "city": "San Francisco"},
    "Oakland Athletics":     {"factor": 0.93, "name": "Sutter Health Park",    "city": "Sacramento"},
}

# Home team → park lookup
_TEAM_TO_PARK = PARK_FACTORS  # keyed by home team name


def get_park_factor(home_team_name: str) -> dict:
    """Return park factor dict for the home team."""
    for team, info in PARK_FACTORS.items():
        if team.lower() in home_team_name.lower() or home_team_name.lower() in team.lower():
            return {"team": team, **info}
    return {"factor": 1.00, "name": "Unknown Park", "city": "Unknown", "team": home_team_name}


# ── Weather ───────────────────────────────────────────────────────────────────

def get_weather(city: str) -> dict:
    """
    Fetch current weather for a city via wttr.in.
    Returns temp_f, wind_mph, wind_dir_label, condition, carries (bool).
    """
    cache_file = CACHE_DIR / f"weather_{city.lower().replace(' ', '_')}.json"
    # Weather cache TTL: 30 min
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 1800:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        r = requests.get(
            f"https://wttr.in/{city.replace(' ', '+')}",
            params={"format": "j1"},
            headers={"User-Agent": "VortexPropEngine/1.0"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Weather fetch failed for %s: %s", city, exc)
        return {}

    try:
        current = data["current_condition"][0]
        temp_f  = int(current["temp_F"])
        wind_mph = int(current["windspeedMiles"])
        wind_dir = current["winddir16Point"]   # e.g. "NW", "SE"
        desc     = current["weatherDesc"][0]["value"].lower()
        carries  = temp_f >= 75
        hot      = temp_f >= 85

        result = {
            "temp_f":    temp_f,
            "wind_mph":  wind_mph,
            "wind_dir":  wind_dir,
            "desc":      desc,
            "carries":   carries,
            "hot":       hot,
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f)
        return result
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Weather parse error: %s", exc)
        return {}


def weather_note(weather: dict, wind_dir: str = None) -> str:
    """Build a human-readable weather note."""
    if not weather:
        return ""
    temp = weather.get("temp_f", 0)
    wind = weather.get("wind_mph", 0)
    wdir = weather.get("wind_dir", "")
    carries = weather.get("carries", False)
    hot = weather.get("hot", False)

    notes = []

    # Wind note — direction matters for hitter-friendly vs pitcher-friendly
    # "out" directions (NW,W,SW,NNW,WNW,WSW,SSW) = hitter friendly for most parks
    OUT_DIRS = {"NW", "W", "SW", "NNW", "WNW", "WSW", "SSW", "NNE"}
    IN_DIRS  = {"SE", "E", "NE", "SSE", "ENE", "ESE", "NNE"}

    if wind >= 10:
        if wdir in OUT_DIRS:
            notes.append(f"\U0001f32c️ Hitter-friendly wind — {wind} mph out to cf \U0001f680 \U0001f4a8")
        elif wdir in IN_DIRS:
            notes.append(f"\U0001f32c️ Pitcher-friendly wind — {wind} mph blowing in \U0001f6ab")
        else:
            notes.append(f"\U0001f32c️ {wind} mph {wdir} wind")

    # Temperature note
    if hot and carries:
        notes.append(f"☀️ Hot conditions — hot ({temp}°f) — ball carries \U0001f321️ (ball carries)")
    elif carries:
        notes.append(f"☀️ Warm conditions ({temp}°f) — ball carries")

    return " · ".join(notes)


# ── Baseball Savant: pitch-type BvP splits ────────────────────────────────────

def get_savant_pitch_splits(batter_id: int, pitcher_id: int) -> list[dict]:
    """
    Pull pitch-type batting splits for a batter vs a specific pitcher from Savant.
    Returns list of dicts: {pitch_type, pitch_name, ab, avg, slg, ops, xh_rate, pct_of_arsenal}
    """
    cache_key = f"savant_pitch_{batter_id}_vs_{pitcher_id}_{stats_mlb.SEASON}"
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        time.sleep(DELAY)
        r = requests.get(
            "https://baseballsavant.mlb.com/statcast_search/csv",
            params={
                "hfGT": "R|",  # regular season
                "hfSea": f"{stats_mlb.SEASON}|",
                "player_type": "batter",
                "batters_lookup[]": batter_id,
                "pitchers_lookup[]": pitcher_id,
                "group_by": "name-pitch_type",
                "sort_col": "pitches",
                "sort_order": "desc",
                "type": "details",
            },
            headers={"User-Agent": "VortexPropEngine/1.0"},
            timeout=20,
        )
        r.raise_for_status()
        rows = _parse_savant_csv(r.text)
    except Exception as exc:
        log.warning("Savant pitch-split fetch failed (%s vs %s): %s", batter_id, pitcher_id, exc)
        return []

    result = []
    for row in rows:
        try:
            ab   = int(row.get("ab", 0) or 0)
            if ab < 3:
                continue
            hits = int(row.get("hit", 0) or 0)
            tb   = int(row.get("total_bases", 0) or 0)
            avg  = f".{int(hits/ab*1000):03d}" if ab else ".000"
            slg  = round(tb / ab, 3) if ab else 0.0
            # Label sample size so consumers can't mistake a 4-AB split for
            # a reliable head-to-head trend
            if ab >= 20:
                sample_note = f"Direct H2H matchup data ({ab} AB — reliable sample)"
                data_source = "direct_h2h"
            elif ab >= 10:
                sample_note = f"Direct H2H — small sample ({ab} AB, directional only)"
                data_source = "small_h2h"
            else:
                sample_note = f"Direct H2H — very small sample ({ab} AB, not reliable)"
                data_source = "minimal_h2h"
            result.append({
                "pitch_type":  row.get("pitch_type", ""),
                "pitch_name":  row.get("pitch_name", row.get("pitch_type", "")),
                "ab":          ab,
                "avg":         avg,
                "slg":         slg,
                "pct":         float(row.get("pitch_percent", 0) or 0),
                "data_source": data_source,
                "sample_note": sample_note,
            })
        except (ValueError, ZeroDivisionError):
            continue

    result.sort(key=lambda x: x["ab"], reverse=True)

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


def get_savant_pitcher_arsenal(pitcher_id: int) -> list[dict]:
    """
    Get the pitcher's current-season pitch mix from Savant.
    Returns list sorted by usage: [{pitch_name, pct}, ...]
    """
    cache_file = CACHE_DIR / f"arsenal_{pitcher_id}_{stats_mlb.SEASON}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        time.sleep(DELAY)
        r = requests.get(
            "https://baseballsavant.mlb.com/statcast_search/csv",
            params={
                "hfGT": "R|",
                "hfSea": f"{stats_mlb.SEASON}|",
                "player_type": "pitcher",
                "pitchers_lookup[]": pitcher_id,
                "group_by": "name-pitch_type",
                "sort_col": "pitches",
                "sort_order": "desc",
                "type": "details",
            },
            headers={"User-Agent": "VortexPropEngine/1.0"},
            timeout=20,
        )
        r.raise_for_status()
        rows = _parse_savant_csv(r.text)
    except Exception as exc:
        log.warning("Savant arsenal fetch failed (%s): %s", pitcher_id, exc)
        return []

    result = []
    total_pitches = sum(int(r.get("pitches", 0) or 0) for r in rows)
    for row in rows:
        pitches = int(row.get("pitches", 0) or 0)
        if pitches < 10 or not total_pitches:
            continue
        result.append({
            "pitch_name":  row.get("pitch_name", row.get("pitch_type", "")),
            "pitch_type":  row.get("pitch_type", ""),
            "pitches":     pitches,
            "pct":         round(pitches / total_pitches * 100, 1),
            "data_source": "pitcher_arsenal_league_wide",
            "sample_note": f"Pitcher's {stats_mlb.SEASON} overall pitch mix — not batter-specific",
        })

    result.sort(key=lambda x: x["pct"], reverse=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


def _parse_savant_csv(text: str) -> list[dict]:
    """Parse CSV text into list of dicts."""
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return []
    headers = [h.strip().strip('"') for h in lines[0].split(",")]
    result  = []
    for line in lines[1:]:
        vals = [v.strip().strip('"') for v in line.split(",")]
        if len(vals) == len(headers):
            result.append(dict(zip(headers, vals)))
    return result


def pitch_split_note(pitch_splits: list[dict], arsenal: list[dict]) -> str:
    """
    Build a 'Crushes X AND Y' note from pitch-type BvP splits.
    Only flags pitches that are both high-usage (≥15% of arsenal) and hittable (avg ≥ .280).
    """
    if not pitch_splits or not arsenal:
        return ""

    arsenal_pct = {a["pitch_name"]: a["pct"] for a in arsenal}
    crushes = []
    for sp in pitch_splits:
        pname = sp["pitch_name"]
        ab    = sp["ab"]
        avg_str = sp["avg"]
        slg   = sp["slg"]
        pct   = arsenal_pct.get(pname, sp.get("pct", 0))

        if ab < 5 or pct < 15:
            continue
        try:
            avg_float = float("0" + avg_str) if avg_str.startswith(".") else float(avg_str)
        except ValueError:
            continue
        if avg_float >= 0.280 and slg >= 0.420:
            crushes.append({
                "name": pname,
                "avg": avg_str,
                "slg": f"{slg:.3f}",
                "ab":  ab,
                "pct": round(pct),
            })

    if not crushes:
        return ""

    crushes = crushes[:2]  # max 2 pitches
    max_ab = max(c["ab"] for c in crushes)

    if len(crushes) == 1:
        p = crushes[0]
        note = (f"🎯 Crushes {p['name']} — "
                f"{p['avg']}/{p['slg']} in {p['ab']} ABs")
    else:
        p1, p2 = crushes[0], crushes[1]
        combined_pct = p1["pct"] + p2["pct"]
        note = (f"🎯 Crushes {p1['name']} AND {p2['name']} — "
                f"{p1['name']} ({p1['avg']}/{p1['slg']} in {p1['ab']} ABs), "
                f"{p2['name']} ({p2['avg']}/{p2['slg']} in {p2['ab']} ABs) — "
                f"combined {combined_pct:.0f}% of arsenal")

    # Integrity label: make it clear this is direct H2H data and flag small samples
    if max_ab < 10:
        note += f" *(H2H — very small sample, treat as anecdotal)*"
    elif max_ab < 20:
        note += f" *(H2H — small sample, directional only)*"
    else:
        note += f" *(direct H2H matchup data)*"

    return note


# ── Platoon splits ─────────────────────────────────────────────────────────────

def get_platoon_splits(player_id: int) -> dict:
    """
    Pull batter's splits vs LHP and RHP for the current season.
    Returns {vs_left: {...}, vs_right: {...}}
    """
    cache_file = CACHE_DIR / f"platoon_{player_id}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        time.sleep(DELAY)
        r = requests.get(
            f"{BASE}/people/{player_id}/stats",
            params={
                "stats": "statSplits",
                "group": "hitting",
                "season": 2025,
                "sportId": 1,
                "sitCodes": "vl,vr",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Platoon splits failed for %s: %s", player_id, exc)
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])
    result = {}
    for sp in splits:
        desc = sp.get("split", {}).get("description", "")
        s    = sp.get("stat", {})
        key  = "vs_left" if "Left" in desc else "vs_right" if "Right" in desc else None
        if not key:
            continue
        result[key] = {
            "avg": s.get("avg", ".---"),
            "ops": s.get("ops", ".---"),
            "pa":  int(s.get("plateAppearances", 0)),
            "hr":  int(s.get("homeRuns", 0)),
        }

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


def platoon_note(platoon: dict, pitcher_hand: str, batter_name: str) -> str:
    """Build platoon advantage note."""
    if not platoon or not pitcher_hand:
        return ""
    key = "vs_right" if pitcher_hand == "R" else "vs_left"
    split = platoon.get(key)
    if not split or split.get("pa", 0) < 30:
        return ""
    avg = split["avg"]
    ops = split["ops"]
    pa  = split["pa"]
    hand_str = "RHP" if pitcher_hand == "R" else "LHP"
    try:
        ops_f = float(ops)
        if ops_f >= 0.850:
            return f"🤝 Platoon edge — {avg} ({ops} OPS) vs {hand_str} pitching over {pa} PA"
    except ValueError:
        pass
    return ""


# ── Team OAA (Outs Above Average — defense quality) ──────────────────────────

def get_team_oaa() -> dict[str, int]:
    """
    Fetch team OAA from Baseball Savant for current season.
    Returns {team_name: oaa_value}
    """
    cache_file = CACHE_DIR / "team_oaa.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        time.sleep(DELAY)
        r = requests.get(
            "https://baseballsavant.mlb.com/leaderboards/outs_above_average",
            params={"type": "team", "year": 2025, "split": 0, "min": 0, "pos": "all"},
            headers={"User-Agent": "VortexPropEngine/1.0"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("OAA fetch failed: %s", exc)
        return {}

    result = {}
    for row in (data if isinstance(data, list) else data.get("data", [])):
        team = row.get("team_name_alt") or row.get("team_name") or row.get("name", "")
        oaa  = row.get("outs_above_average") or row.get("oaa", 0)
        if team:
            try:
                result[team] = int(oaa)
            except (ValueError, TypeError):
                pass

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


def defense_note(home_team: str, away_team: str, batter_is_home: bool) -> str:
    """Build a defense quality note based on OAA."""
    oaa_map = get_team_oaa()
    if not oaa_map:
        return ""
    defending_team = away_team if batter_is_home else home_team
    oaa = None
    for team_name, val in oaa_map.items():
        if any(w in defending_team for w in team_name.split()) or \
           any(w in team_name for w in defending_team.split()):
            oaa = val
            break
    if oaa is None:
        return ""
    if oaa <= -8:
        return f"🧤 poor defense ({oaa} OAA) — balls find grass ✓"
    if oaa >= 10:
        return f"🛡️ elite defense (+{oaa} OAA) — fewer balls fall in"
    return ""


# ── Team strikeout rankings ───────────────────────────────────────────────────

def get_team_k_rankings() -> dict[str, dict]:
    """
    Pull team strikeout totals from MLB Stats API, rank 1-30.
    Returns {team_name: {k_total, rank, label}}
    Rank 1 = most Ks (most K-prone lineup = favorable for pitcher props).
    """
    cache_file = CACHE_DIR / "team_k_ranks.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        time.sleep(DELAY)
        r = requests.get(
            f"{BASE}/teams/stats",
            params={
                "stats": "season", "group": "hitting",
                "season": 2025, "sportId": 1,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Team K rankings fetch failed: %s", exc)
        return {}

    teams = []
    for sp in data.get("stats", [{}])[0].get("splits", []):
        team = sp.get("team", {})
        stat = sp.get("stat", {})
        name = team.get("name", "")
        ks   = int(stat.get("strikeOuts", 0))
        if name:
            teams.append({"name": name, "ks": ks})

    teams.sort(key=lambda x: x["ks"], reverse=True)  # rank 1 = most Ks
    result = {}
    for rank, t in enumerate(teams, 1):
        result[t["name"]] = {
            "k_total": t["ks"],
            "rank":    rank,
            "label":   f"#{rank}/30",
        }

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


def team_k_note(opposing_team_name: str) -> str:
    """Return K rank note for pitcher card."""
    ranks = get_team_k_rankings()
    if not ranks:
        return ""
    for name, info in ranks.items():
        if any(w in opposing_team_name for w in name.split()):
            rank = info["rank"]
            label = f"#{rank}/30"
            if rank <= 10:
                return f"Strikeout-prone lineup ({label} in season Ks) — favorable"
            if rank >= 20:
                return f"Contact lineup ({label} in season Ks) — tougher K spot"
            return f"Average K lineup ({label} in season Ks)"
    return ""


# ── Pitcher K game log (for K ladder) ─────────────────────────────────────────

def get_pitcher_k_splits(pitcher_id: int, line: float) -> dict:
    """
    Pull pitcher's K game log and compute L5/L10/L20 hit rates relative to a K line.
    Also computes projected Ks based on recent trend.
    """
    cache_file = CACHE_DIR / f"k_splits_{pitcher_id}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        raw = json.load(open(cache_file, encoding="utf-8"))
    else:
        try:
            time.sleep(DELAY)
            r = requests.get(
                f"{BASE}/people/{pitcher_id}/stats",
                params={"stats": "gameLog", "group": "pitching", "season": 2025, "sportId": 1},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            raw = r.json()
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(raw, f)
        except Exception as exc:
            log.warning("Pitcher K game log failed for %s: %s", pitcher_id, exc)
            return {}

    splits_raw = list(reversed(
        raw.get("stats", [{}])[0].get("splits", [])
    ))
    if not splits_raw:
        return {}

    k_values = [int(g["stat"].get("strikeOuts", 0)) for g in splits_raw]

    def hr(vals, n):
        sample = vals[:n]
        if len(sample) < max(1, n // 2):
            return None
        hits = sum(1 for v in sample if v >= line)
        avg  = round(sum(sample) / len(sample), 1)
        return {"games": len(sample), "hits": hits,
                "rate": round(hits/len(sample)*100, 1), "avg": avg}

    # Projected Ks: weighted average (L5 weighted 60%, L10 40%)
    proj = None
    l5v  = k_values[:5]
    l10v = k_values[:10]
    if l5v:
        avg5  = sum(l5v) / len(l5v)
        avg10 = sum(l10v) / len(l10v) if l10v else avg5
        proj  = round(0.6 * avg5 + 0.4 * avg10, 1)

    return {
        "l5":      hr(k_values, 5),
        "l10":     hr(k_values, 10),
        "l20":     hr(k_values, 20),
        "proj_ks": proj,
        "avg_ks":  round(sum(k_values[:10]) / min(len(k_values), 10), 1) if k_values else None,
    }


# ── BvP vs team (career) ──────────────────────────────────────────────────────

def get_career_vs_team(batter_id: int, team_id: int) -> dict:
    """Pull career stats for a batter vs a specific team."""
    cache_file = CACHE_DIR / f"bvt_{batter_id}_vs_{team_id}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 86400:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        time.sleep(DELAY)
        r = requests.get(
            f"{BASE}/people/{batter_id}/stats",
            params={
                "stats": "vsTeam", "group": "hitting",
                "opposingTeamId": team_id, "sportId": 1,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Career vs team fetch failed: %s", exc)
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return {}

    # Aggregate all seasons
    ab = hits = hr = pa = tb = bb = 0
    for sp in splits:
        s   = sp.get("stat", {})
        ab  += int(s.get("atBats", 0))
        hits += int(s.get("hits", 0))
        hr  += int(s.get("homeRuns", 0))
        pa  += int(s.get("plateAppearances", 0))
        tb  += int(s.get("totalBases", 0))
        bb  += int(s.get("baseOnBalls", 0))

    if ab == 0:
        return {}

    avg = f".{int(hits/ab*1000):03d}"
    slg = round(tb/ab, 3)
    obp = round((hits+bb)/max(pa,1), 3)
    ops = f"{slg+obp:.3f}"

    result = {"ab": ab, "hits": hits, "hr": hr, "avg": avg, "slg": f"{slg:.3f}", "ops": ops, "pa": pa}
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


# ── Full enrichment package ───────────────────────────────────────────────────

def enrich_mlb_card(
    batter_id:    int,
    pitcher_id:   int,
    home_team:    str,
    away_team:    str,
    batter_team:  str,
    pitcher_hand: str,
    home_team_abbr: str = "",
    game_time_utc: str = "",
    game_pk: int | None = None,
) -> dict:
    """
    Assemble all enrichment data for one MLB prop card.
    Call this once per batter/pitcher pair; cache results in the returned dict.
    """
    batter_is_home = batter_team.lower() in home_team.lower() or \
                     home_team.lower() in batter_team.lower()

    park   = get_park_factor(home_team)
    city = park.get("city", home_team)

    # Prefer a game-time, stadium-oriented forecast. MET Norway includes a
    # stale-cache fallback; Open-Meteo is the second independent forecast;
    # wttr.in current conditions remain the final fallback.
    weather = {}
    if home_team_abbr and game_time_utc and game_pk:
        try:
            candidate = stats_mlb.get_commercial_game_weather(
                home_team_abbr, game_time_utc, int(game_pk)
            )
            if candidate and not candidate.get("error"):
                weather = candidate
        except Exception as exc:
            log.warning("MET game-weather fetch failed for %s: %s", home_team_abbr, exc)
    if not weather and home_team_abbr:
        try:
            candidate = stats_mlb.get_game_weather(home_team_abbr, game_time_utc)
            if candidate and not candidate.get("error"):
                weather = candidate
        except Exception as exc:
            log.warning("Open-Meteo weather fetch failed for %s: %s", home_team_abbr, exc)
    if not weather:
        weather = get_weather(city)
    if weather:
        if "speed_mph" not in weather:
            weather["speed_mph"] = weather.get("wind_mph")
        if "wind_mph" not in weather:
            weather["wind_mph"] = weather.get("speed_mph")
    oaa_note = defense_note(home_team, away_team, batter_is_home)

    pitch_splits = get_savant_pitch_splits(batter_id, pitcher_id)
    arsenal      = get_savant_pitcher_arsenal(pitcher_id)
    crush_note   = pitch_split_note(pitch_splits, arsenal)

    platoon  = get_platoon_splits(batter_id)
    plat_note = platoon_note(platoon, pitcher_hand, "")

    return {
        "park":        park,
        "weather":     weather,
        "weather_note": weather_note(weather),
        "arsenal":     arsenal,
        "crush_note":  crush_note,
        "platoon":     platoon,
        "platoon_note": plat_note,
        "defense_note": oaa_note,
    }
