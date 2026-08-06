"""
VORTEX Player Research Engine
==============================
Full research card for any MLB player — works even if they're not on the board.
Fuzzy name search, live splits, pitcher matchup, recent game log, Statcast metrics.
"""
import sys
import time
import requests
from difflib import SequenceMatcher
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import stats_mlb

STAT_TYPES = ["hits", "total_bases", "hits_runs_rbis", "home_runs",
              "rbis", "runs_scored", "walks"]

# Default prop lines per stat — matches typical book lines for hit rate calc
STAT_DEFAULT_LINES = {
    "hits":           0.5,   # did they get 1+ hit?
    "total_bases":    1.5,   # did they get 2+ TB?
    "hits_runs_rbis": 1.5,   # most common book line; override with actual line when known
    "home_runs":      0.5,   # did they hit a HR?
    "rbis":           0.5,   # did they drive in a run?
    "runs_scored":    0.5,   # did they score a run?
    "walks":          0.5,   # did they draw a walk?
}

STAT_LABELS = {
    "hits":           "Hits",
    "total_bases":    "Total Bases",
    "hits_runs_rbis": "Hits+Runs+RBIs",
    "home_runs":      "Home Runs",
    "rbis":           "RBIs",
    "runs_scored":    "Runs Scored",
    "walks":          "Walks",
}

THUMBNAIL_URL = (
    "https://img.mlbstatic.com/mlb-photos/image/upload/"
    "d_people:generic:headshot:67:current.png/"
    "w_213,q_auto:best/v1/people/{player_id}/headshot/67/current"
)

NAME_ALIASES = {
    "alex": "alexander",
    "mike": "michael",
    "max": "maxwell",
    "nick": "nicholas",
    "zack": "zachary",
    "rob": "robert",
    "bob": "robert",
    "tom": "thomas",
    "tim": "timothy",
    "dan": "daniel",
    "jon": "jonathan",
    "joe": "joseph",
    "jim": "james",
    "chris": "christopher",
    "ben": "benjamin",
    "ed": "edward",
    "matt": "matthew",
    "greg": "gregory",
    "jeff": "jeffrey",
    "brad": "bradley",
    "cal": "calvin",
    "lew": "lewis",
    "sam": "samuel",
}

_PLAYER_LIST_CACHE: tuple[float, list[dict]] = (0.0, [])
_ROSTER_CACHE_SECONDS = 30 * 60


def get_active_player_list() -> list[dict]:
    """Return the active MLB player list, refreshed every 30 minutes."""
    global _PLAYER_LIST_CACHE
    cached_at, cached = _PLAYER_LIST_CACHE
    if cached and time.time() - cached_at < _ROSTER_CACHE_SECONDS:
        return cached
    try:
        r = requests.get(f"{stats_mlb.BASE}/sports/1/players",
                         params={"season": 2026, "hydrate": "currentTeam"},
                         timeout=15)
        r.raise_for_status()
        players = r.json().get("people", [])
        _PLAYER_LIST_CACHE = (time.time(), players)
        return players
    except Exception:
        return cached or []


_TEAM_IDS: dict[int, str] = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC", 119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD", 136: "SEA", 137: "SF", 138: "STL",
    139: "TB", 140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

def search_players_local(query: str, limit: int = 25) -> list[dict]:
    """Fast local player search over cached list. Returns [{id, name, team}]."""
    players = get_active_player_list()
    if not players or not query.strip():
        return []
    q = _norm_name(query)
    scored = []
    for p in players:
        name = p.get("fullName", "")
        n = _norm_name(name)
        ratio = SequenceMatcher(None, q, n).ratio()
        starts = n.startswith(q)
        contains = q in n
        if starts or contains or ratio >= 0.55:
            team = ""
            ct = p.get("currentTeam")
            if ct:
                team = ct.get("abbreviation") or ""
                if not team and "id" in ct:
                    team = _TEAM_IDS.get(ct["id"], "")
                if not team:
                    team = ct.get("name", "")
            scored.append({"id": p["id"], "name": name, "team": team, "_score": (starts, contains, ratio)})
    scored.sort(key=lambda x: x["_score"], reverse=True)
    return [{"id": s["id"], "name": s["name"], "team": s["team"]} for s in scored[:limit]]


def _norm_name(name: str) -> str:
    parts = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in name).split()
    if parts:
        parts[0] = NAME_ALIASES.get(parts[0], parts[0])
    return " ".join(parts)


def fuzzy_search(query: str) -> list[dict]:
    """
    Search MLB API for players matching a partial name.
    Returns list of {id, name, team, position, active} dicts.
    Tries multiple name formats (Last/First swap, dotted initials) before giving up.
    Falls back to lenient fuzzy matching across all active players if API fails.
    """
    BASE = stats_mlb.BASE
    fatal_errors: list[str] = []

    def _api(name: str) -> list:
        try:
            r = requests.get(f"{BASE}/people/search",
                             params={"names": name, "sportId": 1, "hydrate": "currentTeam"},
                             timeout=10)
            r.raise_for_status()
            return r.json().get("people", [])
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in (404, 406):
                fatal_errors.append(f"{type(exc).__name__}: {exc}")
            return []
        except Exception as exc:
            fatal_errors.append(f"{type(exc).__name__}: {exc}")
            return []

    def _active_players() -> list[dict]:
        global _PLAYER_LIST_CACHE
        cached_at, cached = _PLAYER_LIST_CACHE
        if cached and time.time() - cached_at < _ROSTER_CACHE_SECONDS:
            return cached
        try:
            r = requests.get(f"{BASE}/sports/1/players",
                             params={"season": 2026, "hydrate": "currentTeam"},
                             timeout=15)
            r.raise_for_status()
            players = r.json().get("people", [])
            _PLAYER_LIST_CACHE = (time.time(), players)
            return players
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in (404, 406):
                fatal_errors.append(f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            fatal_errors.append(f"{type(exc).__name__}: {exc}")
        return []

    parts      = query.strip().split()
    query_last = parts[-1].lower() if parts else ""

    def _last_matches(p: dict) -> bool:
        fp = (p.get("fullName") or "").split()
        return bool(fp) and fp[-1].lower() == query_last

    # Try exact API searches first
    people = _api(query)

    if not people and len(parts) >= 2:
        people = _api(f"{parts[-1]}, {parts[0]}")

    if not people and len(parts) >= 2:
        first = parts[0]
        if len(first) <= 3 and first.isalpha() and first.isupper():
            dotted = ".".join(list(first)) + "."
            people = _api(f"{dotted} {' '.join(parts[1:])}")

    if not people and query_last:
        people = _api(query_last)

    # Fuzzy match fallback 1: active players with strict last name match
    if not people and len(parts) >= 2:
        q_norm = _norm_name(query)
        people = [
            p for p in _active_players()
            if _last_matches(p) and SequenceMatcher(None, q_norm, _norm_name(p.get("fullName", ""))).ratio() >= 0.70
        ]

    # Fuzzy match fallback 2: all active players if last name match failed
    # This handles OCR errors that change the last name (e.g., "Taytor" instead of "Taylor")
    if not people and len(parts) >= 2:
        q_norm = _norm_name(query)
        candidates = [
            p for p in _active_players()
            if SequenceMatcher(None, q_norm, _norm_name(p.get("fullName", ""))).ratio() >= 0.65
        ]
        # Sort by match quality and take top matches
        if candidates:
            def _similarity(p):
                return SequenceMatcher(None, q_norm, _norm_name(p.get("fullName", ""))).ratio()
            candidates.sort(key=_similarity, reverse=True)
            people = candidates[:5]

    if not people and fatal_errors:
        raise RuntimeError(f"MLB player search unavailable ({fatal_errors[-1]})")

    # Prefer last-name-matching results to avoid cross-player mismatches
    matched = [p for p in people if _last_matches(p)]
    pool    = matched if matched else people

    q_norm = _norm_name(query)

    def _score(p: dict) -> tuple:
        name = p.get("fullName", "")
        n_norm = _norm_name(name)
        ratio = SequenceMatcher(None, q_norm, n_norm).ratio()
        same_last = 1 if _last_matches(p) else 0
        first_ok = 0
        if q_norm.split() and n_norm.split():
            first_ok = 1 if q_norm.split()[0] == n_norm.split()[0] else 0
        active_ok = 1 if p.get("active") else 0
        return (same_last, first_ok, active_ok, ratio)

    pool = sorted(pool, key=_score, reverse=True)
    active   = [p for p in pool if p.get("active")]
    inactive = [p for p in pool if not p.get("active")]
    results  = []
    for p in (active + inactive)[:5]:
        results.append({
            "id":       p["id"],
            "name":     p.get("fullName", ""),
            "team":     p.get("currentTeam", {}).get("name", ""),
            "position": p.get("primaryPosition", {}).get("abbreviation", ""),
            "active":   p.get("active", False),
        })
    return results


def _is_pitcher(profile: dict) -> bool:
    """Return True if the player's primary position is pitcher."""
    pos = profile.get("primaryPosition", {}).get("abbreviation", "")
    return pos in ("P", "SP", "RP", "CP")


def get_pitcher_card(player_id: int, profile: dict) -> dict:
    """Research card for a pitcher — shows pitching stats, K/9, ERA, recent starts."""
    name    = profile.get("fullName", "Unknown")
    hand    = profile.get("pitchHand", {}).get("description", "")
    pos     = profile.get("primaryPosition", {}).get("name", "")

    metrics = stats_mlb.get_pitcher_metrics(name) or {}

    # Find the pitcher in today's schedule by matching their name as a probable pitcher.
    # This is more reliable than profile.currentTeam which can show rehab assignments.
    schedule    = stats_mlb.get_todays_schedule()
    game_info   = {}
    opp_team_id = None
    team        = profile.get("currentTeam", {}).get("name", "")
    team_id     = profile.get("currentTeam", {}).get("id")

    for game in schedule.values():
        hp = game.get("home_pitcher", "") or ""
        ap = game.get("away_pitcher", "") or ""
        if name.lower() in hp.lower() or hp.lower() in name.lower():
            # Pitcher is home starter
            team        = game.get("home_team_name", team)
            team_id     = game.get("home_team_id", team_id)
            opp_team_id = game.get("away_team_id")
            game_info   = {"opponent": game.get("away_team_name", ""), "is_home": True}
            break
        if name.lower() in ap.lower() or ap.lower() in name.lower():
            # Pitcher is away starter
            team        = game.get("away_team_name", team)
            team_id     = game.get("away_team_id", team_id)
            opp_team_id = game.get("home_team_id")
            game_info   = {"opponent": game.get("home_team_name", ""), "is_home": False}
            break

    # L5 K average + trend vs season
    last5    = metrics.get("last_5_starts") or []
    l5_ks    = [s.get("k", 0) for s in last5[:5]]
    l5_k_avg = round(sum(l5_ks) / len(l5_ks), 1) if l5_ks else None

    # Season K per start average
    season_ks = metrics.get("season_ks", 0)
    gs        = metrics.get("games_started", 0)
    k_per_gs  = round(season_ks / gs, 1) if gs else None

    # K hit rate vs common lines from full game log
    k_hit_rates = _get_pitcher_k_hit_rates(player_id)

    # Opponent team strikeout rate vs LHP specifically
    opp_k_rate = _get_team_k_rate(opp_team_id) if opp_team_id else {}
    opp_vs_lhp = _get_team_vs_handedness(opp_team_id, "L") if opp_team_id and hand == "Left" else {}
    opp_vs_rhp = _get_team_vs_handedness(opp_team_id, "R") if opp_team_id and hand != "Left" else {}

    # Top lineup BvP — key hitters from tonight's opponent vs this pitcher
    lineup_bvp = _get_lineup_bvp(opp_team_id, player_id, schedule) if opp_team_id else []

    # Career vs tonight's opponent team
    career_vs_team = {}
    if opp_team_id:
        career_vs_team = _get_pitcher_vs_team(player_id, opp_team_id)

    # Home/Away ERA split
    home_away_era = _get_pitcher_home_away(player_id)

    return {
        "player_id":       player_id,
        "name":            name,
        "team":            team,
        "position":        pos,
        "hand":            hand,
        "is_pitcher":      True,
        "metrics":         metrics,
        "game_info":       game_info,
        "opp_team_id":     opp_team_id,
        "l5_k_avg":        l5_k_avg,
        "k_per_gs":        k_per_gs,
        "k_hit_rates":     k_hit_rates,
        "opp_k_rate":      opp_k_rate,
        "opp_vs_hand":     opp_vs_lhp or opp_vs_rhp,
        "lineup_bvp":      lineup_bvp,
        "career_vs_team":  career_vs_team,
        "home_away_era":   home_away_era,
        "thumbnail_url":   THUMBNAIL_URL.format(player_id=player_id),
    }


def _get_all_teams_k_rate() -> dict:
    """Fetch all MLB teams' K% this season in one call. Returns {team_id: k_pct}."""
    data = stats_mlb._get(
        "/teams/stats",
        {"stats": "season", "group": "hitting",
         "season": stats_mlb.SEASON, "sportId": 1},
        cache_key=f"all_teams_hit_{stats_mlb.SEASON}",
    )
    if not data:
        return {}
    result = {}
    for split in (data.get("stats") or [{}])[0].get("splits", []):
        tid = split.get("team", {}).get("id")
        s   = split.get("stat", {})
        pa  = int(s.get("plateAppearances", 1)) or 1
        ks  = int(s.get("strikeOuts", 0))
        if tid:
            result[tid] = {
                "k_pct": round(ks / pa * 100, 1),
                "avg":   s.get("avg", ".---"),
                "ks":    ks,
                "pa":    pa,
            }
    return result


def _get_team_k_rate(team_id: int) -> dict:
    """Get opposing team's strikeout rate + league ranking (1 = most Ks)."""
    all_teams = _get_all_teams_k_rate()
    if not all_teams:
        # Fallback: single team fetch
        data = stats_mlb._get(
            f"/teams/{team_id}/stats",
            {"stats": "season", "group": "hitting",
             "season": stats_mlb.SEASON, "sportId": 1},
            cache_key=f"team_hit_{team_id}_{stats_mlb.SEASON}",
        )
        if not data:
            return {}
        splits = (data.get("stats") or [{}])[0].get("splits", [])
        if not splits:
            return {}
        s   = splits[0].get("stat", {})
        pa  = int(s.get("plateAppearances", 1)) or 1
        ks  = int(s.get("strikeOuts", 0))
        return {"k_pct": round(ks / pa * 100, 1), "avg": s.get("avg", ".---"),
                "ks": ks, "pa": pa, "rank": None, "total_teams": 30}

    team_data = all_teams.get(team_id, {})
    if not team_data:
        return {}

    # Rank: 1 = hardest to K (lowest K rate), 30 = easiest to K (highest K rate)
    sorted_teams = sorted(all_teams.items(), key=lambda x: x[1]["k_pct"], reverse=False)
    rank = next((i + 1 for i, (tid, _) in enumerate(sorted_teams) if tid == team_id), None)

    return {
        "k_pct":       team_data["k_pct"],
        "avg":         team_data["avg"],
        "ks":          team_data["ks"],
        "pa":          team_data["pa"],
        "rank":        rank,
        "total_teams": len(sorted_teams),
    }


def _get_pitcher_vs_team(pitcher_id: int, opp_team_id: int) -> dict:
    """Career pitching stats vs opposing team."""
    data = stats_mlb._get(
        f"/people/{pitcher_id}/stats",
        {"stats": "vsTeam", "group": "pitching",
         "opposingTeamId": opp_team_id, "sportId": 1},
        cache_key=f"pitcher_vs_team_{pitcher_id}_{opp_team_id}",
    )
    if not data:
        return {}
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    return {
        "era":  s.get("era", "-.--"),
        "ip":   s.get("inningsPitched", "0.0"),
        "k":    int(s.get("strikeOuts", 0)),
        "bb":   int(s.get("baseOnBalls", 0)),
        "avg":  s.get("avg", ".---"),
    }


def _get_pitcher_k_hit_rates(pitcher_id: int) -> dict:
    """
    From the pitcher's full game log, compute hit rates over common K lines.
    Returns {line: {l5, l10, l20}} for lines 4.5, 5.5, 6.5, 7.5.
    """
    data = stats_mlb._get(
        f"/people/{pitcher_id}/stats",
        {"stats": "gameLog", "group": "pitching",
         "season": stats_mlb.SEASON, "sportId": 1},
        cache_key=f"gamelog_pitch_{pitcher_id}_{stats_mlb.SEASON}",
    )
    if not data:
        return {}
    raw   = (data.get("stats") or [{}])[0].get("splits", [])
    games = list(reversed(raw))  # newest first
    if not games:
        return {}

    k_vals = [int(g.get("stat", {}).get("strikeOuts", 0)) for g in games]

    result = {}
    for line in [4.5, 5.5, 6.5, 7.5]:
        rates = {}
        for n, label in [(5, "l5"), (10, "l10"), (20, "l20")]:
            sample = k_vals[:n]
            if len(sample) < max(1, n // 2):
                continue
            hits = sum(1 for k in sample if k > line)
            rates[label] = {
                "hits":  hits,
                "games": len(sample),
                "rate":  round(hits / len(sample) * 100, 0),
            }
        if rates:
            result[str(line)] = rates
    return result


def _get_team_vs_handedness(team_id: int, hand: str) -> dict:
    """Get team batting stats vs LHP or RHP this season."""
    site_code = "vl" if hand == "L" else "vr"
    data = stats_mlb._get(
        f"/teams/{team_id}/stats",
        {"stats": "statSplits", "group": "hitting",
         "season": stats_mlb.SEASON, "sitCodes": site_code, "sportId": 1},
        cache_key=f"team_vs_{hand}_{team_id}_{stats_mlb.SEASON}",
    )
    if not data:
        return {}
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s  = splits[0].get("stat", {})
    pa = int(s.get("plateAppearances", 1)) or 1
    ks = int(s.get("strikeOuts", 0))
    return {
        "k_pct": round(ks / pa * 100, 1),
        "avg":   s.get("avg", ".---"),
        "ops":   s.get("ops", ".---"),
        "pa":    pa,
        "hand":  hand,
    }


def _get_lineup_bvp(opp_team_id: int, pitcher_id: int, schedule: dict) -> list[dict]:
    """
    Get top hitters from tonight's opponent roster and their career K rate vs this pitcher.
    Returns top 5 hitters with meaningful AB history sorted by K count desc.
    """
    # Get opponent roster (active batters)
    data = stats_mlb._get(
        f"/teams/{opp_team_id}/roster",
        {"rosterType": "active", "season": stats_mlb.SEASON},
        cache_key=f"roster_{opp_team_id}_{stats_mlb.SEASON}",
    )
    if not data:
        return []

    players = []
    for p in (data.get("roster") or []):
        pos = p.get("position", {}).get("abbreviation", "")
        if pos in ("P", "TWP"):
            continue
        players.append({
            "id":   p.get("person", {}).get("id"),
            "name": p.get("person", {}).get("fullName", ""),
        })

    results = []
    for p in players[:15]:  # check first 15 position players
        if not p["id"]:
            continue
        bvp = stats_mlb.get_bvp_history(p["id"], pitcher_id)
        if bvp.get("ab", 0) >= 5:
            results.append({
                "name": p["name"],
                "ab":   bvp["ab"],
                "k":    bvp["k"],
                "avg":  bvp["avg"],
                "hr":   bvp["hr"],
                "k_pct": round(bvp["k"] / bvp["ab"] * 100, 0) if bvp["ab"] else 0,
            })

    # Sort by AB descending (most history = most reliable)
    results.sort(key=lambda x: x["ab"], reverse=True)
    return results[:6]


def _get_pitcher_home_away(pitcher_id: int) -> dict:
    """Home vs away ERA splits for a pitcher."""
    data = stats_mlb._get(
        f"/people/{pitcher_id}/stats",
        {"stats": "statSplits", "group": "pitching",
         "season": stats_mlb.SEASON, "sitCodes": "h,a", "sportId": 1},
        cache_key=f"pitcher_homeaway_{pitcher_id}_{stats_mlb.SEASON}",
    )
    if not data:
        return {}
    result = {}
    for split in (data.get("stats") or [{}])[0].get("splits", []):
        code = split.get("split", {}).get("code", "")
        s    = split.get("stat", {})
        if code == "h":
            result["home_era"] = s.get("era", "-.--")
            result["home_ip"]  = s.get("inningsPitched", "0.0")
        elif code == "a":
            result["away_era"] = s.get("era", "-.--")
            result["away_ip"]  = s.get("inningsPitched", "0.0")
    return result


def get_research_card(player_id: int, stat_type: str = "hits_runs_rbis",
                      line: float = 1.5) -> dict:
    """Full research card for a player."""
    profile = stats_mlb._get_player_profile(player_id)
    if not profile:
        return {"error": "Player profile not found"}

    # Route pitchers to a separate card
    if _is_pitcher(profile):
        return get_pitcher_card(player_id, profile)

    name     = profile.get("fullName", "Unknown")
    team     = profile.get("currentTeam", {}).get("name", "")
    team_id  = profile.get("currentTeam", {}).get("id")
    team_abbr= profile.get("currentTeam", {}).get("abbreviation", "")
    position = profile.get("primaryPosition", {}).get("name", "")
    bat_side = profile.get("batSide", {}).get("description", "")

    # Splits for all stat types — use stat-appropriate default lines
    all_splits = {}
    primary_splits = None
    for st in STAT_TYPES:
        try:
            st_line = STAT_DEFAULT_LINES.get(st, line)
            s = stats_mlb.get_historical_splits(player_id, st_line, st)
            if "error" not in s:
                all_splits[st] = s
                if st == stat_type:
                    primary_splits = s
        except Exception:
            pass

    if primary_splits is None:
        primary_splits = all_splits.get(STAT_TYPES[0], {})

    # Recent game log — last 10 games
    recent_games = _get_recent_games(player_id, stat_type, n=10)

    # Today's pitcher
    schedule       = stats_mlb.get_todays_schedule()
    pitcher_name   = None
    pitcher_data   = {}
    direct_pit_id  = None   # ID direct from schedule — no name-search risk
    if team_id:
        name_lookup = stats_mlb.build_pitcher_lookup(schedule)
        id_lookup   = stats_mlb.build_pitcher_id_lookup(schedule)
        pitcher_name  = name_lookup.get(team_id)
        direct_pit_id = id_lookup.get(team_id)
        if pitcher_name:
            pitcher_data = stats_mlb.get_pitcher_metrics(pitcher_name) or {}

    # BvP — use schedule pitcher ID directly to avoid name-search mismatch.
    # If the name search returned a different pitcher than the schedule has,
    # direct_pit_id wins (it came straight from the MLB schedule API).
    bvp = {}
    bvp_pitcher_id = direct_pit_id or pitcher_data.get("pitcher_id")
    if bvp_pitcher_id:
        bvp = stats_mlb.get_bvp_history(player_id, bvp_pitcher_id) or {}

    # Batter's own stats vs pitcher's arm-side — used to contextualize platoon.
    batter_vs_hand = {}
    _pit_hand = pitcher_data.get("hand", "")
    if _pit_hand in ("L", "R"):
        try:
            batter_vs_hand = stats_mlb.get_batter_hand_splits(player_id, _pit_hand) or {}
        except Exception:
            pass

    # Today's game info
    game_info = {}
    opp_team_id = None
    for game in schedule.values():
        if game.get("home_team_id") == team_id:
            game_info = {"opponent": game.get("away_team_name", ""),
                         "opp_abbr": game.get("away_team_abbr", ""),
                         "is_home": True}
            opp_team_id = game.get("away_team_id")
            break
        if game.get("away_team_id") == team_id:
            game_info = {"opponent": game.get("home_team_name", ""),
                         "opp_abbr": game.get("home_team_abbr", ""),
                         "is_home": False}
            opp_team_id = game.get("home_team_id")
            break

    # Season avgs for all stats
    season_summary = {}
    for st, sp in all_splits.items():
        season_summary[st] = {
            "avg":    sp.get("season_avg"),
            "label":  STAT_LABELS.get(st, st),
            "games":  sp.get("games_played"),
        }

    # Season slash line (BA/OBP/SLG/OPS) from MLB Stats API
    slash_line = _get_season_slash_line(player_id)

    # Home/Away splits for selected stat_type
    home_away = _get_home_away_splits(player_id, stat_type, recent_games)

    # Statcast metrics from Baseball Savant
    statcast = _get_statcast(player_id, name)

    # Career vs tonight's opponent
    career_vs_team = {}
    if opp_team_id:
        career_vs_team = _get_career_vs_team(player_id, opp_team_id)

    # L10 breakdown by individual stats (H, HR, RBI, R, TB)
    l10_breakdown = _get_l10_breakdown(recent_games)

    return {
        "player_id":      player_id,
        "name":           name,
        "team":           team,
        "team_id":        team_id,
        "team_abbr":      team_abbr,
        "position":       position,
        "bat_side":       bat_side,
        "stat_type":      stat_type,
        "splits":         primary_splits,
        "all_splits":     all_splits,
        "season_summary": season_summary,
        "recent_games":   recent_games,
        "pitcher":        pitcher_data,
        "pitcher_name":   pitcher_name,
        "bvp":            bvp,
        "game_info":      game_info,
        "opp_team_id":    opp_team_id,
        "slash_line":     slash_line,
        "home_away":      home_away,
        "statcast":       statcast,
        "career_vs_team": career_vs_team,
        "l10_breakdown":  l10_breakdown,
        "batter_vs_hand": batter_vs_hand,
        "thumbnail_url":  THUMBNAIL_URL.format(player_id=player_id),
    }


def _get_recent_games(player_id: int, stat_type: str, n: int = 10) -> list[dict]:
    """Pull last N game log entries with full stat breakdown."""
    from datetime import date as _date
    _today = _date.today().isoformat()
    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "gameLog", "group": "hitting",
         "season": stats_mlb.SEASON, "sportId": 1},
        cache_key=f"gamelog_hit_{player_id}_{stats_mlb.SEASON}_{_today}",
    )
    if not data:
        return []

    raw   = (data.get("stats") or [{}])[0].get("splits", [])
    games = list(reversed(raw))[:n]

    result = []
    for g in games:
        s    = g.get("stat", {})
        opp  = g.get("opponent", {}).get("name", "")
        opp_abbr = g.get("opponent", {}).get("abbreviation", "")
        date = g.get("date", "")
        home_away_flag = g.get("isHome", None)
        result.append({
            "date":        date,
            "opponent":    opp,
            "opp_abbr":    opp_abbr,
            "is_home":     home_away_flag,
            "h":           s.get("hits", 0),
            "r":           s.get("runs", 0),
            "rbi":         s.get("rbi", 0),
            "hr":          s.get("homeRuns", 0),
            "tb":          s.get("totalBases", 0),
            "bb":          s.get("baseOnBalls", 0),
            "k":           s.get("strikeOuts", 0),
            "hrr":         s.get("hits", 0) + s.get("runs", 0) + s.get("rbi", 0),
            "summary":     s.get("summary", ""),
            "stat_value":  stats_mlb._stat_from_game(s, stat_type),
        })
    return result


def _get_season_slash_line(player_id: int) -> dict:
    """Get BA/OBP/SLG/OPS from MLB season stats."""
    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "season", "group": "hitting",
         "season": stats_mlb.SEASON, "sportId": 1},
        cache_key=f"season_slash_{player_id}_{stats_mlb.SEASON}",
    )
    if not data:
        return {}
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    return {
        "avg":  s.get("avg", ".---"),
        "obp":  s.get("obp", ".---"),
        "slg":  s.get("slg", ".---"),
        "ops":  s.get("ops", ".---"),
        "ab":   s.get("atBats", 0),
        "gp":   s.get("gamesPlayed", 0),
        "sb":   s.get("stolenBases", 0),
    }


def _get_home_away_splits(player_id: int, stat_type: str,
                           recent_games: list[dict]) -> dict:
    """
    Compute home/away per-game averages from the recent game log.
    Falls back to stat-level MLB API split if needed.
    """
    home_vals, away_vals = [], []
    for g in recent_games:
        val = g.get("stat_value", 0)
        if g.get("is_home") is True:
            home_vals.append(val)
        elif g.get("is_home") is False:
            away_vals.append(val)

    def _avg(vals):
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    return {
        "home_avg":    _avg(home_vals),
        "home_games":  len(home_vals),
        "away_avg":    _avg(away_vals),
        "away_games":  len(away_vals),
    }


_savant_cache: dict = {}

def _get_savant_leaderboard() -> dict:
    """
    Download the Baseball Savant statcast leaderboard once per day.
    Returns {player_name_lower: {ev, barrel_pct, hard_hit_pct, sweet_spot_pct}}.
    """
    import csv, io
    from datetime import date
    today = date.today().isoformat()
    cache_key = f"savant_leaderboard_{today}"
    cache_file = stats_mlb.CACHE_DIR / f"{cache_key}.json"

    if cache_file.exists():
        import json
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    try:
        r = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/statcast",
            params={"type": "batter", "year": stats_mlb.SEASON,
                    "position": "", "team": "", "min": 5, "csv": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return {}
        text   = r.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        result = {}
        for row in reader:
            raw_name = row.get("last_name, first_name", "")
            if not raw_name:
                continue
            parts = [p.strip() for p in raw_name.split(",")]
            if len(parts) == 2:
                name_key = f"{parts[1]} {parts[0]}".lower()
            else:
                name_key = raw_name.lower()
            try:
                result[name_key] = {
                    "exit_velocity":   float(row.get("avg_hit_speed") or 0) or None,
                    "hard_hit_pct":    float(row.get("ev95percent") or 0) or None,
                    "barrel_pct":      float(row.get("brl_percent") or 0) or None,
                    "sweet_spot_pct":  float(row.get("anglesweetspotpercent") or 0) or None,
                    "max_ev":          float(row.get("max_hit_speed") or 0) or None,
                    "player_id":       row.get("player_id", "").strip(),
                }
            except (ValueError, TypeError):
                continue
        if result:
            import json
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result, f)
        return result
    except Exception:
        return {}


def _get_statcast(player_id: int, player_name: str = "") -> dict:
    """
    Fetch Statcast metrics (EV, Barrel%, Hard Hit%) from Baseball Savant leaderboard.
    Matches by player name since Savant IDs can differ from MLB Stats API IDs.
    """
    leaderboard = _get_savant_leaderboard()
    if not leaderboard:
        return {}

    name_key = player_name.lower().strip()
    data = leaderboard.get(name_key)

    # Fuzzy fallback — try matching last name
    if not data and name_key:
        last = name_key.split()[-1]
        candidates = [v for k, v in leaderboard.items() if last in k]
        if len(candidates) == 1:
            data = candidates[0]

    return data or {}


def _get_career_vs_team(player_id: int, opp_team_id: int) -> dict:
    """Career batting stats vs tonight's opponent team."""
    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "vsTeam", "group": "hitting",
         "opposingTeamId": opp_team_id, "sportId": 1},
        cache_key=f"vs_team_{player_id}_{opp_team_id}",
    )
    if not data:
        return {}
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    return {
        "avg": s.get("avg", ".---"),
        "ops": s.get("ops", ".---"),
        "ab":  s.get("atBats", 0),
        "hr":  s.get("homeRuns", 0),
        "rbi": s.get("rbi", 0),
    }


def _get_l10_breakdown(recent_games: list[dict]) -> dict:
    """Compute L10 per-game averages for each key stat."""
    games = recent_games[:10]
    if not games:
        return {}
    n = len(games)
    return {
        "h":   round(sum(g["h"]   for g in games) / n, 2),
        "hr":  round(sum(g["hr"]  for g in games) / n, 2),
        "rbi": round(sum(g["rbi"] for g in games) / n, 2),
        "r":   round(sum(g["r"]   for g in games) / n, 2),
        "tb":  round(sum(g["tb"]  for g in games) / n, 2),
        "bb":  round(sum(g["bb"]  for g in games) / n, 2),
        "games": n,
    }
