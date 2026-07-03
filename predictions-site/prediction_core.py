"""
Vortex — live prediction core (shared by every serverless platform wrapper)
=============================================================================
Computes a real /prediction-style breakdown for an arbitrary
player/stat/line/side on demand — no database, no odds API.

Reuses the same MLB Stats API wrappers (backend/stats_mlb.py,
backend/research.py) and scoring engine (backend/analyze.py
grade_pick_both) as the Discord bot, so scores/tiers are identical.
The narrative text below is a purpose-built formatter for this
endpoint (NOT extracted from the ~1500-line Discord embed builder,
to avoid risking any change to the live bot's behavior) closely
modeled on the bot's wording.

Platform-specific entry points (netlify/functions/prediction.py,
api/prediction.py for Vercel) just parse their platform's request
shape and call compute_prediction() below — all logic lives here once.

Local dev / smoke test: python prediction_core.py "<player>" "<stat label>" <line> <over|under>
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Serverless platforms (Vercel, and drag-and-drop deploys especially) only
# bundle files inside the deployed project root -- backend/ living one level
# up in the real repo would be silently excluded. predictions-site/backend/
# is a bundled COPY kept in sync manually for deployability; it's preferred
# whenever present. Falls back to the real ../backend/ for local dev so
# editing the actual bot code still works without a copy step, as long as
# you're running from a full repo checkout (not a stripped-down deploy dir).
_BUNDLED_BACKEND_DIR = Path(__file__).resolve().parent / "backend"
_REPO_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
_BACKEND_DIR = _BUNDLED_BACKEND_DIR if _BUNDLED_BACKEND_DIR.exists() else _REPO_BACKEND_DIR
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_BACKEND_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR.parent))

import analyze          # backend/analyze.py — grade_pick_both, compute_hit_rates, PROP_STAT_MAP helpers
import stats_mlb         # backend/stats_mlb.py
import research as vortex_research  # backend/research.py — fuzzy_search

# MLB's 30 franchise team IDs are permanent -- used as a fallback when the
# API's currentTeam hydration omits "name" for a player (a real, observed
# quirk; e.g. Michael Busch's record only carried currentTeam.id).
_MLB_TEAM_ID_TO_NAME = {
    108: "Los Angeles Angels", 109: "Arizona Diamondbacks", 110: "Baltimore Orioles",
    111: "Boston Red Sox", 112: "Chicago Cubs", 113: "Cincinnati Reds",
    114: "Cleveland Guardians", 115: "Colorado Rockies", 116: "Detroit Tigers",
    117: "Houston Astros", 118: "Kansas City Royals", 119: "Los Angeles Dodgers",
    120: "Washington Nationals", 121: "New York Mets", 133: "Athletics",
    134: "Pittsburgh Pirates", 135: "San Diego Padres", 136: "Seattle Mariners",
    137: "San Francisco Giants", 138: "St. Louis Cardinals", 139: "Tampa Bay Rays",
    140: "Texas Rangers", 141: "Toronto Blue Jays", 142: "Minnesota Twins",
    143: "Philadelphia Phillies", 144: "Atlanta Braves", 145: "Chicago White Sox",
    146: "Miami Marlins", 147: "New York Yankees", 158: "Milwaukee Brewers",
}

# Maps the human-readable stat labels the website shows to the internal
# prop_type keys backend/stats_mlb.py and backend/analyze.py expect.
STAT_LABEL_TO_PROP_TYPE = {
    "Hits": "hits",
    "Total Bases": "total_bases",
    "Home Runs": "home_runs",
    "RBIs": "rbis",
    "Runs Scored": "runs_scored",
    "Strikeouts": "strikeouts",
    "Walks": "walks",
    "Hits+Runs+RBIs": "hits_runs_rbis",
}


class PlayerNotFound(Exception):
    pass


class NoGameFound(Exception):
    pass


def search_players(query: str, limit: int = 8) -> list[dict]:
    """
    Autocomplete lookup for the search box, matching against ANY part of a
    player's name (first, last, or full) so "michael" finds "Michael Busch",
    not just players with that exact last name.

    Deliberately does NOT use research.fuzzy_search() for this -- that
    endpoint (MLB's /people/search) is built for resolving one specific,
    mostly-complete name and can return a single loosely-related match
    (including minor-leaguers) for a short/ambiguous query like "michael".
    Instead this pulls the full current-season active-roster list (~1275
    real MLB players on an actual 40-man roster, file-cached ~48h since it
    barely changes intra-day) and does substring matching ourselves, which
    is both more forgiving and guaranteed to only surface real MLB players.
    """
    query = (query or "").strip().lower()
    if len(query) < 2:
        return []

    data = stats_mlb._get(
        "/sports/1/players",
        {"season": stats_mlb.SEASON, "hydrate": "currentTeam"},
        cache_key=f"active_roster_{stats_mlb.SEASON}",
    )
    people = (data or {}).get("people", [])

    matches = []
    for p in people:
        current_team = p.get("currentTeam") or {}
        if not current_team.get("id"):
            continue  # no current MLB team = not actually available to research tonight
        # currentTeam sometimes omits "name" even when hydrated (API quirk) --
        # fall back to the stable team-id table rather than skipping the player.
        team = current_team.get("name") or _MLB_TEAM_ID_TO_NAME.get(current_team["id"], "")
        if not team:
            continue
        full_name = p.get("fullName", "")
        first = p.get("firstName", "")
        last = p.get("lastName", "")
        if query in full_name.lower() or query in first.lower() or query in last.lower():
            # Prefix matches on a name part rank above mid-string matches
            # (e.g. "jud" -> Judge ranks above a hypothetical "Majudsky").
            is_prefix = any(part.lower().startswith(query) for part in (first, last) if part)
            matches.append({
                "id": p["id"],
                "name": full_name,
                "team": team,
                "position": (p.get("primaryPosition") or {}).get("abbreviation", ""),
                "_rank": 0 if is_prefix else 1,
            })

    matches.sort(key=lambda m: (m["_rank"], m["name"]))
    for m in matches:
        del m["_rank"]
    return matches[:limit]


def _safe(fn, *args, default=None):
    """Runs a stats_mlb fetch inside a thread pool, swallowing any single
    signal's failure so one flaky endpoint doesn't take down the whole
    lookup — matches the try/except-per-call behavior the sequential
    version had, just under concurrent execution."""
    try:
        result = fn(*args)
        return result if result is not None else default
    except Exception:
        return default


def compute_prediction(player_name: str, prop_type: str, stat_label: str, line: float, side: str) -> dict:
    matches = vortex_research.fuzzy_search(player_name)
    if not matches:
        raise PlayerNotFound(f"Couldn't find an MLB player matching \"{player_name}\".")
    found = matches[0]
    player_id = found["id"]
    canonical_name = found.get("name", player_name)
    team_abbr = found.get("team", "")

    matchup = analyze.get_matchup_info(player_id)
    if not matchup:
        reason = analyze.get_no_game_reason(player_id)
        raise NoGameFound(reason or f"No upcoming game found for {canonical_name}.")

    # "Strikeouts" is a PITCHER prop (how many Ks they throw), not a batter
    # stat — completely different pipeline. Splits come from the pitching
    # log, matchup grading is vs the OPPOSING LINEUP's K-rate (not a single
    # opposing pitcher), and none of the batter-vs-pitcher context (BvP,
    # handedness splits, arsenal fit) applies since this player IS the
    # pitcher tonight, not a hitter facing one.
    if prop_type == "strikeouts":
        return compute_k_prop(player_id, canonical_name, team_abbr, matchup, line, side, stat_label)

    is_home = bool(matchup.get("is_home"))
    # PARK_FACTOR is keyed by the HOME team's name — that's the batter's own
    # team when they're home, otherwise the opponent (whose park it is tonight).
    home_team_name = (found.get("team") or "") if is_home else (matchup.get("opponent") or "")
    park_factor = stats_mlb.PARK_FACTOR.get(home_team_name, 1.0)
    home_abbr = stats_mlb._MLB_TEAM_ABBR.get(home_team_name, "")
    opp_team_id = matchup.get("opp_team_id")
    home_team_id = matchup.get("home_team_id")
    pitcher_name = matchup.get("pitcher") or ""

    # Wave 1: every fetch that only needs player_id/opp_team_id/matchup info
    # (not the opposing pitcher's resolved ID) — these are all independent
    # network calls, so run them concurrently instead of one after another.
    # This is the same ~11 signals the sequential version fetched, just in
    # parallel; a cold-cache lookup used to take 3-6s serialized, now bounded
    # by the single slowest call instead of their sum.
    with ThreadPoolExecutor(max_workers=11) as pool:
        f_splits = pool.submit(analyze.compute_hit_rates, player_id, line, prop_type)
        f_pitcher = pool.submit(_safe, stats_mlb.get_pitcher_metrics, pitcher_name, default={}) if pitcher_name else None
        f_hand_splits = pool.submit(_safe, stats_mlb.get_batter_hand_splits, player_id, default={})
        f_statcast = pool.submit(_safe, stats_mlb.get_statcast_by_id, player_id, default={})
        f_vs_team = pool.submit(_safe, stats_mlb.get_vs_team_splits, player_id, opp_team_id, line, prop_type, default={}) if opp_team_id else None
        f_weather = pool.submit(_safe, stats_mlb.get_game_weather, home_abbr, matchup.get("game_utc", ""), default={}) if home_abbr else None
        f_team_bvp = pool.submit(_safe, stats_mlb.get_team_bvp, player_id, opp_team_id, default={}) if opp_team_id else None
        f_oaa = pool.submit(_safe, stats_mlb.get_team_defense_oaa, opp_team_id, default={}) if opp_team_id else None
        f_k_rates = pool.submit(_safe, stats_mlb.get_all_teams_k_rate, default={})
        f_lineup_spot = pool.submit(_safe, stats_mlb.get_lineup_position, player_id, default=None)
        f_umpire = pool.submit(_safe, stats_mlb.get_game_umpire, home_team_id, default={}) if home_team_id else None

        splits = f_splits.result()
        if splits.get("error"):
            raise NoGameFound(splits["error"])

        pitcher = f_pitcher.result() if f_pitcher else {}
        if pitcher.get("error"):
            pitcher = {}
        hand_splits = f_hand_splits.result()
        statcast = f_statcast.result()
        vs_team = f_vs_team.result() if f_vs_team else {}
        weather = f_weather.result() if f_weather else {}
        team_bvp = f_team_bvp.result() if f_team_bvp else {}
        oaa = f_oaa.result() if f_oaa else {}
        k_rates = f_k_rates.result()
        lineup_spot = f_lineup_spot.result() if f_lineup_spot else None
        umpire = f_umpire.result() if f_umpire else {}

    opp_k_rank, opp_k_pct = None, None
    if opp_team_id:
        opp_k = k_rates.get(opp_team_id) or k_rates.get(str(opp_team_id))
        if opp_k:
            opp_k_rank = opp_k.get("rank")
            # get_all_teams_k_rate returns k_pct as a percentage (e.g. 22.7);
            # grade_pick() expects a 0.0-1.0 fraction.
            raw_k_pct = opp_k.get("k_pct")
            opp_k_pct = (raw_k_pct / 100) if raw_k_pct is not None else None

    # Wave 2: signals that need the opposing pitcher's resolved ID from wave 1.
    bvp, arsenal, bat_vs_pitch = None, [], []
    pitcher_id = pitcher.get("pitcher_id")
    if pitcher_id:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_bvp = pool.submit(_safe, stats_mlb.get_bvp_history, player_id, pitcher_id, default={})
            f_arsenal = pool.submit(_safe, stats_mlb.get_pitcher_arsenal, pitcher_id, default=[])
            f_bat_vs_pitch = pool.submit(_safe, stats_mlb.get_batter_vs_pitch_type, player_id, pitcher_id, default=[])

            bvp_raw = f_bvp.result()
            if not bvp_raw.get("error"):
                bvp = bvp_raw
            arsenal = f_arsenal.result() or []
            bat_vs_pitch = f_bat_vs_pitch.result() or []

    grade = analyze.grade_pick_both(
        splits=splits,
        line=line,
        opp_k_rank=opp_k_rank,
        opp_k_pct=opp_k_pct,
        pitcher=pitcher or None,
        bvp=bvp,
        park_factor=park_factor,
        weather=weather or None,
        team_bvp=team_bvp or None,
        oaa=oaa or None,
        prop_type=prop_type,
        lineup_spot=lineup_spot,
        statcast=statcast or None,
        team_h2h=vs_team or None,
        arsenal=arsenal or None,
        bat_vs_pitch=bat_vs_pitch or None,
        vs_hand_splits=hand_splits or None,
        umpire=umpire or None,
    )

    picked_grade = grade["over_grade"] if side == "over" else grade["under_grade"]
    picked_score = grade["over_score"] if side == "over" else grade["under_score"]

    return format_response(
        player_name=canonical_name,
        team_abbr=team_abbr,
        headshot=f"https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_auto:best/v1/people/{player_id}/headshot/67/current",
        stat_label=stat_label,
        prop_type=prop_type,
        line=line,
        side=side,
        splits=splits,
        matchup=matchup,
        pitcher=pitcher,
        bvp=bvp,
        hand_splits=hand_splits,
        park_factor=park_factor,
        statcast=statcast,
        arsenal=arsenal,
        vs_team=vs_team,
        team_bvp=team_bvp,
        weather=weather,
        umpire=umpire,
        grade=grade,
        picked_grade=picked_grade,
        picked_score=picked_score,
    )


def compute_k_prop(player_id, canonical_name, team_abbr, matchup, line, side, stat_label) -> dict:
    opp_team_id = matchup.get("opp_team_id")
    is_home = bool(matchup.get("is_home"))
    home_team_name = (team_abbr or "") if is_home else (matchup.get("opponent") or "")
    home_abbr = stats_mlb._MLB_TEAM_ABBR.get(home_team_name, "")
    park_factor = stats_mlb.PARK_FACTOR.get(home_team_name, 1.0)

    # k_card is the one expensive call (season stats + pitching log + opponent
    # K-rate); weather, the pitcher's own arsenal (their whiff weapons), and
    # the plate ump are all unrelated to it, so run them side by side.
    home_team_id = matchup.get("home_team_id")
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_k_card = pool.submit(stats_mlb.get_pitcher_k_card, canonical_name, line, opp_team_id, pitcher_id=player_id)
        f_weather = pool.submit(_safe, stats_mlb.get_game_weather, home_abbr, matchup.get("game_utc", ""), default={}) if home_abbr else None
        f_arsenal = pool.submit(_safe, stats_mlb.get_pitcher_arsenal, player_id, default=[])
        f_umpire = pool.submit(_safe, stats_mlb.get_game_umpire, home_team_id, default={}) if home_team_id else None
        k_card = f_k_card.result()
        weather = f_weather.result() if f_weather else {}
        arsenal = f_arsenal.result() or []
        umpire = f_umpire.result() if f_umpire else {}

    if k_card.get("error"):
        raise NoGameFound(
            f"{k_card['error']} — {canonical_name} may not be pitching tonight, "
            "or hasn't started this season."
        )

    splits = dict(k_card.get("splits") or {})
    splits["recent_games"] = [
        {
            "date": s.get("date", ""),
            "opponent": s.get("opponent", ""),
            "value": s.get("k", 0),
        }
        for s in (k_card.get("last_5_starts") or [])
    ]

    opp_k = k_card.get("opp_k") or {}
    opp_k_rank = opp_k.get("rank")
    raw_k_pct = opp_k.get("k_pct")
    opp_k_pct = (raw_k_pct / 100) if raw_k_pct is not None else None

    grade = analyze.grade_pick_both(
        splits=splits,
        line=line,
        opp_k_rank=opp_k_rank,
        opp_k_pct=opp_k_pct,
        # Deliberately NOT passing `pitcher=k_card` here: grade_pick()'s ERA-ladder
        # branch reads pitcher.get("era") etc. at the top level for an OPPOSING
        # pitcher's quality in a batter prop -- k_card nests those under
        # "season_stats" instead, so passing it as-is would read era=0 and
        # wrongly trigger the "elite ace suppression" bonus. That whole branch
        # doesn't apply anyway: this player IS the pitcher being graded, not an
        # opponent to grade against.
        pitcher=None,
        park_factor=park_factor,
        prop_type="strikeouts",
    )
    picked_grade = grade["over_grade"] if side == "over" else grade["under_grade"]
    picked_score = grade["over_score"] if side == "over" else grade["under_score"]

    return format_k_prop_response(
        player_name=canonical_name,
        team_abbr=team_abbr,
        headshot=f"https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_auto:best/v1/people/{player_id}/headshot/67/current",
        stat_label=stat_label,
        line=line,
        side=side,
        splits=splits,
        matchup=matchup,
        k_card=k_card,
        opp_k=opp_k,
        park_factor=park_factor,
        weather=weather,
        arsenal=arsenal,
        umpire=umpire,
        grade=grade,
        picked_grade=picked_grade,
        picked_score=picked_score,
    )


def format_k_prop_response(*, player_name, team_abbr, headshot, stat_label, line, side, splits,
                            matchup, k_card, opp_k, park_factor, weather, grade, picked_grade, picked_score,
                            arsenal=None, umpire=None) -> dict:
    is_under = side == "under"
    season = k_card.get("season_stats") or {}
    opponent = matchup.get("opponent", "")
    is_home = bool(matchup.get("is_home"))
    location = "🏠 Home" if is_home else "✈️ Away"

    def _for_side(block):
        block = block or {}
        games = block.get("games") or 0
        over_hits = block.get("hits") or 0
        if not games:
            return {"games": 0, "hits": 0, "rate": None, "avg": block.get("avg")}
        hits = (games - over_hits) if is_under else over_hits
        return {"games": games, "hits": hits, "rate": round(hits / games * 100), "avg": block.get("avg")}

    l5 = _for_side(splits.get("l5"))
    l10 = _for_side(splits.get("l10"))
    l20 = _for_side(splits.get("l20"))
    l10_rate = l10["rate"] or 0
    l10_avg = l10.get("avg") or 0
    edge = round((line - l10_avg) if is_under else (l10_avg - line), 2)

    # Core projection: k_per_9 scaled to a recent-form innings estimate, then
    # adjusted by how the opposing lineup's K-rate compares to league average.
    why_it_hits = []
    try:
        k9 = float(season.get("k_per_9") or 0)
        recent_starts = k_card.get("last_5_starts") or []
        outs = [s.get("outs", 0) for s in recent_starts if s.get("outs")]
        proj_ip = round(sum(outs) / len(outs) / 3, 1) if outs else None
        league_avg_k_pct = 0.220
        matchup_factor = round((opp_k.get("k_pct", 22.0) / 100) / league_avg_k_pct, 3) if opp_k.get("k_pct") is not None else 1.0
        if k9 and proj_ip:
            proj_ks = round(k9 * proj_ip / 9 * matchup_factor, 1)
            direction = "OVER" if proj_ks > line else "UNDER" if proj_ks < line else "PUSH"
            why_it_hits.append(
                f"🔍 Core projection: {direction} {line} — model projects {proj_ks} Ks "
                f"({k9} K/9 × {matchup_factor} factor × {proj_ip} IP)."
            )
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    if l10.get("games"):
        why_it_hits.append(
            f"Has hit {side.title()} {line} in {l10['hits']}/{l10['games']} of the last 10 starts ({l10_rate}%)."
        )
    if l5.get("rate") is not None:
        why_it_hits.append(f"L5: {l5['hits']}/{l5['games']} ({l5['rate']}%).")

    if opp_k:
        rank, pct = opp_k.get("rank"), opp_k.get("k_pct")
        if rank and pct is not None:
            if pct > 22.0:
                favors_text = "favors the OVER"
            elif pct < 20.0:
                favors_text = "favors the UNDER"
            else:
                favors_text = "doesn't strongly favor either side"
            why_it_hits.append(f"{opponent} ranks #{rank}/30 in K rate ({pct}%) — {favors_text}.")

    season_ip_per_gs = None
    try:
        ip_raw = str(season.get("innings_pitched") or "0.0")
        whole, frac = ip_raw.split(".")
        ip_dec = float(whole) + float(frac) / 3
        gs = season.get("games_started") or 0
        if gs:
            season_ip_per_gs = round(ip_dec / gs, 1)
    except (ValueError, ZeroDivisionError):
        pass
    if season_ip_per_gs:
        if season_ip_per_gs >= 5.5:
            why_it_hits.append(f"Deep outings — {season_ip_per_gs} IP avg (3x through the lineup) — max K opportunity.")
        else:
            why_it_hits.append(f"Short leash — {season_ip_per_gs} IP avg limits total K opportunity.")

    tier_label = picked_grade.get("label", "Lean")
    tier_icon = picked_grade.get("emoji", "➡️")

    negative_signals = list(picked_grade.get("penalty_desc") or [])

    risk = list(negative_signals)
    if not risk:
        risk.append("No major red flags in available data.")
    stability = picked_grade.get("stability_tier")
    if stability:
        unstable = stability.upper() in ("LOW", "VOLATILE")
        risk.insert(0, f"Stability: {stability.title()} — {'K totals swing widely start to start' if unstable else 'consistent K output'}.")
    risk.append("Live lookup — sample sizes and matchup data are current as of this request.")

    dist_values = (splits.get("l20") or {}).get("values") or (splits.get("l10") or {}).get("values") or []
    distribution = _build_distribution(dist_values, line, strict_over=True)

    wind_text = ""
    if weather and weather.get("speed_mph") is not None and not weather.get("dome"):
        wind_text = f"Wind: {weather['speed_mph']} mph {weather.get('effect', '')}".strip() + "."

    last5_display = [
        {"value": s.get("k", 0), "opponent": stats_mlb._MLB_TEAM_ABBR.get(s.get("opponent", ""), (s.get("opponent") or "")[:3].upper()), "date": _short_date(s.get("date", ""))}
        for s in (k_card.get("last_5_starts") or [])
    ][::-1]

    return {
        "id": f"live-{player_name.lower().replace(' ', '-')}-strikeouts-{side}-{line}",
        "player": player_name,
        "team": team_abbr,
        "headshot": headshot,
        "sport": "MLB",
        "betType": stat_label,
        "line": line,
        "side": side.title(),
        "score": picked_score,
        "tier": tier_label,
        "tierIcon": tier_icon,
        "estHitRate": l10_rate,
        "location": location,
        "unitSize": _unit_size_for(tier_label),
        "verdict": f"{side.upper()} {line}",
        "verdictDetail": (
            f"L10 hit rate: {l10_rate}% · L10 avg: {l10_avg} vs {line} line "
            f"({'+' if edge >= 0 else ''}{edge}) · Stability: {(picked_grade.get('stability_tier') or '—').title()} · "
            f"Projection edge: {'+' if picked_grade.get('proj_edge', 0) >= 0 else ''}{picked_grade.get('proj_edge', 0)} vs line"
        ),
        "whyItHits": why_it_hits,
        "hitRates": {"l5": l5["rate"] or 0, "l10": l10_rate, "l20": l20["rate"] or 0},
        "last5": last5_display,
        "split": {
            "roadAvg": None,
            "roadOverRate": None,
            "homeAvg": None,
            "homeOverRate": None,
            "callout": "Split data unavailable",
            "volume": f"Season avg {season.get('k_per_gs', '—')} K/start over {season.get('games_started', '—')} GS.",
        },
        "matchup": {
            "opponent": opponent,
            "pitcher": (
                f"{player_name} — {season.get('era', '—')} ERA · {season.get('k_per_9', '—')} K/9 · "
                f"{season.get('k_per_gs', '—')} K/start avg"
            ),
            # No BvP/handedness here on purpose -- those describe how THIS player
            # hits against an opposing pitcher, which is meaningless when they're
            # the one pitching. The opposing lineup's K-rate above is the real signal.
            "bvp": None,
            "bvpNote": None,
            "leash": "",
            "handedness": "",
        },
        "narrative": (
            f"{player_name} has hit {side.title()} {line} in {l10.get('hits', 0)}/{l10.get('games', 0)} "
            f"of the last 10 starts ({l10_rate}%), averaging {l10_avg} strikeouts per start.\n\n"
            f"{opponent} ranks #{opp_k.get('rank', '—')}/30 in K rate ({opp_k.get('k_pct', '—')}%) tonight.\n\n"
            f"{'The evidence stacks toward the ' + side.title() + '.' if picked_score > 0 else 'The signals here are mixed — treat with caution.'}"
        ),
        "seasonLine": f"Season {season.get('k_per_9', '—')} K/9 over {season.get('games_started', '—')} starts",
        "vsMatchup": {
            "h2h": None,
            "h2hNote": None,
            "career": (
                f"Home ERA {k_card.get('home_era')} · Away ERA {k_card.get('away_era')}"
                if k_card.get("home_era") is not None or k_card.get("away_era") is not None else ""
            ),
            "season": f"Last 5 starts: " + "  ".join(f"{s.get('k', 0)}K" for s in (k_card.get("last_5_starts") or [])),
        },
        "environment": (
            _park_factor_label(park_factor, is_under, "strikeouts") + " "
            + ("Indoor — weather N/A." if weather.get("dome") else "")
            + ((" " + _umpire_line(umpire)) if _umpire_line(umpire) else "")
        ).strip(),
        "wind": wind_text,
        "risk": risk,
        "negativeSignals": negative_signals,
        "confidenceBreakdown": _build_confidence_breakdown(picked_grade),
        "distribution": distribution,
        # This player's OWN arsenal -- the whiff weapons driving the K total.
        "pitchArsenal": _format_arsenal(arsenal),
        "pitchArsenalLabel": f"{player_name}'s arsenal",
        "modelConfirm": (
            f"Over score {grade['over_score']} · Under score {grade['under_score']} · "
            f"Confidence: {round(grade['confidence'] * 100)}%"
        ),
        "date": "",
    }


def format_response(*, player_name, team_abbr, headshot, stat_label, prop_type, line, side, splits,
                     matchup, pitcher, bvp, hand_splits, park_factor, statcast, arsenal, vs_team, team_bvp, weather,
                     grade, picked_grade, picked_score, umpire=None) -> dict:
    # stats_mlb's l5/l10/l20 blocks always report the OVER side (hits = games
    # where value >= line). Flip hits/rate for an Under lookup so the display
    # actually reflects the side being shown, not always the Over numbers.
    is_under = side == "under"

    def _for_side(block):
        block = block or {}
        games = block.get("games") or 0
        over_hits = block.get("hits") or 0
        if not games:
            return {"games": 0, "hits": 0, "rate": None, "avg": block.get("avg")}
        hits = (games - over_hits) if is_under else over_hits
        return {"games": games, "hits": hits, "rate": round(hits / games * 100), "avg": block.get("avg")}

    l5 = _for_side(splits.get("l5"))
    l10 = _for_side(splits.get("l10"))
    l20 = _for_side(splits.get("l20"))

    is_home = bool(matchup.get("is_home"))
    location = "🏠 Home" if is_home else "✈️ Away"
    opponent = matchup.get("opponent", "")

    l10_rate = l10["rate"] or 0
    l10_avg = l10.get("avg") or 0
    edge = round((line - l10_avg) if is_under else (l10_avg - line), 2)

    why_it_hits = []
    if l10.get("games"):
        why_it_hits.append(
            f"Has hit {side.title()} {line} in "
            f"{l10['hits']}/{l10['games']} of the last 10 games "
            f"({l10_rate}%)."
        )
    if l5.get("rate") is not None:
        why_it_hits.append(f"L5: {l5['hits']}/{l5['games']} ({l5['rate']}%).")
    if l20.get("rate") is not None:
        why_it_hits.append(f"L20: {l20['hits']}/{l20['games']} ({l20['rate']}%).")
    if pitcher.get("era"):
        why_it_hits.append(
            f"Facing {pitcher.get('name', matchup.get('pitcher', ''))} "
            f"({pitcher.get('hand', '?')}HP) — {pitcher.get('era')} ERA · "
            f"{pitcher.get('hr_per_9')} HR/9."
        )

    # xSLG/xwOBA contact quality
    xslg_f, xwoba_f = _to_float(statcast.get("xslg")), _to_float(statcast.get("xwoba"))
    if xslg_f is not None and xwoba_f is not None:
        quality = "elite" if xwoba_f >= 0.370 else "above-average" if xwoba_f >= 0.330 else "below-average"
        why_it_hits.append(f"xSLG {xslg_f} · xwOBA {xwoba_f} — {quality} contact quality.")

    # Streak (stats_mlb's raw l10 "streak" is always Over-signed: + = Over streak, - = Under)
    raw_l10 = splits.get("l10") or {}
    raw_streak = raw_l10.get("streak") or 0
    side_streak = -raw_streak if is_under else raw_streak
    if side_streak >= 3:
        why_it_hits.append(f"🔥 {side_streak}-game {side.title()} streak — prop has hit {side_streak} straight.")

    # L3 vs season average — is he heating up or cooling off right now?
    recent = splits.get("recent_games") or []
    season_avg = splits.get("season_avg")
    if len(recent) >= 3 and season_avg is not None:
        l3_avg = round(sum(g.get("value", 0) for g in recent[:3]) / 3, 2)
        diff = round(l3_avg - season_avg, 2)
        trend = "spiking in recent sample" if diff > 0 else "dipping in recent sample" if diff < 0 else "matching season pace"
        why_it_hits.append(f"L3 avg {l3_avg} vs {season_avg} season avg ({'+' if diff >= 0 else ''}{diff}) — {trend}.")

    # Pitcher's primary pitches
    if arsenal:
        top = sorted(arsenal, key=lambda a: a.get("pct", 0), reverse=True)[:2]
        if top:
            pitch_str = " · ".join(f"{p.get('pitch_name', '?')} ({round(p.get('pct', 0))}%)" for p in top)
            why_it_hits.append(f"Primary pitches: {pitch_str}")

    bvp_line, bvp_note = None, None
    if bvp and bvp.get("ab"):
        bvp_line = _bvp_line(bvp)
    if bvp and bvp.get("ab", 0) >= 3:
        bvp_note = _bvp_verdict(bvp, player_name, pitcher.get("name") or matchup.get("pitcher"), is_under)

    handedness_text = None
    p_hand = pitcher.get("hand")
    if p_hand and hand_splits and hand_splits.get(p_hand):
        hs = hand_splits[p_hand]
        handedness_text = (
            f"This pitcher throws {'right' if p_hand == 'R' else 'left'}-handed. "
            f"{player_name.split()[-1]} hits {p_hand}HP at {hs.get('avg', '.---')} AVG / "
            f"{hs.get('ops', '.---')} OPS ({hs.get('pa', 0)} PA)."
        )

    leash_text = ""
    avg_ip_l3 = pitcher.get("avg_ip_l3")
    if avg_ip_l3 is not None:
        leash_text = (
            f"Short leash — {avg_ip_l3} IP avg → bullpen by {int(avg_ip_l3) + 1}th inning."
            if avg_ip_l3 < 5.0 else
            f"Long leash — {avg_ip_l3} IP avg lets him work deep into games."
        )

    vs_team_text = ""
    if vs_team and vs_team.get("games"):
        side_hits = vs_team["under"] if is_under else vs_team["over"]
        side_rate = vs_team["under_rate"] if is_under else vs_team["over_rate"]
        # Matches build_analyze_embed()'s exact small-sample threshold (backend/analyze.py ~2921).
        sample_note = "" if vs_team["games"] >= 5 else f" — small sample ({vs_team['games']}g)"
        vs_team_text = (
            f"{side.title()} {line} vs {vs_team['team_name']} this season: "
            f"{side_hits}/{vs_team['games']} hit ({side_rate}%) · avg {vs_team.get('avg', '—')}{sample_note}"
        )

    wind_text = ""
    if weather and weather.get("speed_mph") is not None and not weather.get("dome"):
        wind_text = f"Wind: {weather['speed_mph']} mph {weather.get('effect', '')}".strip() + "."

    tier_label = picked_grade.get("label", "Lean")
    tier_icon = picked_grade.get("emoji", "➡️")

    negative_signals = list(picked_grade.get("penalty_desc") or [])

    risk = list(negative_signals)
    if not risk:
        risk.append("No major red flags in available data.")
    stability = picked_grade.get("stability_tier")
    if stability:
        unstable = stability.upper() in ("LOW", "VOLATILE")
        risk.insert(0, f"Stability: {stability.title()} — {'inconsistent recent values, treat hit rate with caution' if unstable else 'consistent recent output'}.")
    risk.append("Live lookup — sample sizes and matchup data are current as of this request.")

    dist_values = (splits.get("l20") or {}).get("values") or (splits.get("l10") or {}).get("values") or []
    distribution = _build_distribution(dist_values, line)

    # Confirmed lineup spot + the model's projected plate appearances for it
    # (both already computed inside grade_pick when the lineup is posted).
    lineup_spot = picked_grade.get("lineup_spot")
    proj_pa = picked_grade.get("proj_pa")
    lineup_text = ""
    if lineup_spot:
        ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(lineup_spot, f"{lineup_spot}th")
        lineup_text = f"Batting {ordinal} tonight" + (f" — ~{proj_pa} projected PA" if proj_pa else "") + "."

    return {
        "id": f"live-{player_name.lower().replace(' ', '-')}-{prop_type}-{side}-{line}",
        "player": player_name,
        "team": team_abbr,
        "headshot": headshot,
        "sport": "MLB",
        "betType": stat_label,
        "line": line,
        "side": side.title(),
        "score": picked_score,
        "tier": tier_label,
        "tierIcon": tier_icon,
        "estHitRate": l10_rate,
        "location": location,
        "unitSize": _unit_size_for(tier_label),
        "verdict": f"{side.upper()} {line}",
        "verdictDetail": (
            f"L10 hit rate: {l10_rate}% · L10 avg: {l10_avg} vs {line} line "
            f"({'+' if edge >= 0 else ''}{edge}) · Stability: {(picked_grade.get('stability_tier') or '—').title()} · "
            f"Projection edge: {'+' if picked_grade.get('proj_edge', 0) >= 0 else ''}{picked_grade.get('proj_edge', 0)} vs line"
            + (f" · Damage: {_damage_label(picked_grade.get('damage_score'))}" if picked_grade.get("damage_score") is not None else "")
        ),
        "whyItHits": why_it_hits,
        "hitRates": {
            "l5": l5["rate"] or 0,
            "l10": l10_rate,
            "l20": l20["rate"] or 0,
        },
        "last5": [
            {
                "value": g.get("value", 0),
                "opponent": stats_mlb._MLB_TEAM_ABBR.get(g.get("opponent", ""), (g.get("opponent") or "")[:3].upper()),
                "date": _short_date(g.get("date", "")),
            }
            for g in (splits.get("recent_games") or [])[:5]
        ][::-1],
        "split": {
            "roadAvg": splits.get("away_avg"),
            "roadOverRate": splits.get("away_rate"),
            "homeAvg": splits.get("home_avg"),
            "homeOverRate": splits.get("home_rate"),
            "callout": _split_callout(splits, is_home),
            "volume": f"Season avg {splits.get('season_avg', '—')} over {splits.get('games_played', '—')} GP.",
        },
        "matchup": {
            "opponent": opponent,
            "pitcher": (
                f"Facing {pitcher.get('name', matchup.get('pitcher', ''))} "
                f"({pitcher.get('hand', '?')}HP) — {pitcher.get('era', '—')} ERA · "
                f"{pitcher.get('hr_per_9', '—')} HR/9 · {pitcher.get('fip', '—')} FIP"
                if pitcher else ""
            ),
            "bvp": bvp_line,
            "bvpNote": bvp_note,
            "leash": leash_text,
            "handedness": handedness_text or "",
            "lineup": lineup_text,
        },
        "narrative": (
            f"{player_name} has hit {side.title()} {line} in {l10.get('hits', 0)}/{l10.get('games', 0)} "
            f"of the last 10 games ({l10_rate}%), averaging {l10_avg} {stat_label} per game — "
            f"{'above' if edge >= 0 else 'below'} tonight's {line} line.\n\n"
            f"{'The evidence stacks toward the ' + side.title() + '.' if picked_score > 0 else 'The signals here are mixed — treat with caution.'}"
        ),
        "seasonLine": f"Season avg {splits.get('season_avg', '—')} over {splits.get('games_played', '—')} GP",
        "vsMatchup": {
            "h2h": bvp_line,
            "h2hNote": bvp_note,
            "career": (
                f"Career vs {opponent}: {team_bvp['avg']} avg / {team_bvp['pa']} PA · OPS {team_bvp['ops']}"
                if team_bvp and team_bvp.get("pa") else ""
            ),
            "season": vs_team_text,
        },
        "environment": (
            _park_factor_label(park_factor, is_under, prop_type) + " "
            + ("Indoor — weather N/A." if weather.get("dome") else "")
            + ((" " + _umpire_line(umpire)) if _umpire_line(umpire) else "")
        ).strip(),
        "wind": wind_text,
        "risk": risk,
        "negativeSignals": negative_signals,
        "confidenceBreakdown": _build_confidence_breakdown(picked_grade),
        "distribution": distribution,
        # The OPPOSING pitcher's arsenal -- what this batter has to hit tonight.
        "pitchArsenal": _format_arsenal(arsenal),
        "pitchArsenalLabel": (f"{pitcher.get('name', 'Opposing pitcher')}'s arsenal" if pitcher else "Opposing pitcher's arsenal"),
        "modelConfirm": (
            f"Over score {grade['over_score']} · Under score {grade['under_score']} · "
            f"Confidence: {round(grade['confidence'] * 100)}%"
        ),
        "date": "",
    }


def _split_callout(splits: dict, is_home: bool) -> str:
    home_avg, away_avg = splits.get("home_avg"), splits.get("away_avg")
    if home_avg is None or away_avg is None:
        return ""
    stronger_home = home_avg >= away_avg
    tonight_is_stronger = stronger_home == is_home
    return (
        "Tonight's location is the stronger split — tailwind."
        if tonight_is_stronger else
        "The other venue has actually been stronger this season — monitor carefully."
    )


def _bvp_line(bvp: dict) -> str:
    """Matches the bot's BvP formatting: '2/10 (.200 AVG · 0.750 OPS) · 3 K · 1 HR — moderate sample'."""
    ab = bvp.get("ab", 0) or 0
    segments = [f"{bvp.get('hits', 0)}/{ab} ({bvp.get('avg', '.---')} AVG · {bvp.get('ops', '.---')} OPS)"]
    if bvp.get("k"):
        segments.append(f"{bvp['k']} K")
    if bvp.get("hr"):
        segments.append(f"{bvp['hr']} HR")
    sample = (
        "large sample" if ab >= 20 else
        "moderate sample" if ab >= 10 else
        "small sample" if ab >= 5 else
        "very small sample — treat with caution"
    )
    return " · ".join(segments) + f" — {sample}"


def _park_factor_label(park_factor: float, is_under: bool, prop_type: str = "") -> str:
    """Mirrors build_analyze_embed()'s environment section exactly
    (backend/analyze.py ~line 2936) -- polarity/icon flips for Under bets
    and for strikeout props, which react to park factor differently
    (a hitter-friendly park means deeper counts, not more offense)."""
    if not park_factor or park_factor == 1.0:
        return "Neutral park (1.00x)."

    if prop_type == "strikeouts":
        if park_factor >= 1.05:
            note = "longer at-bats may suppress Ks" if is_under else "more offense = deeper counts = K chances"
            return f"Hitter-friendly park ({park_factor:.2f}x) — {note}."
        if park_factor <= 0.95:
            note = "suppressed offense supports pitcher dominance" if not is_under else "pitchers tend to go deeper here = more K exposure"
            return f"Pitcher-friendly park ({park_factor:.2f}x) — {note}."
        return f"Neutral park ({park_factor:.2f}x)."

    if park_factor >= 1.05:
        note = "boosts production" if not is_under else "elevates risk for Under"
        return f"Hitter-friendly park ({park_factor:.2f}x) — {note}."
    if park_factor <= 0.95:
        note = "suppresses offense" if not is_under else "supports Under"
        return f"Pitcher-friendly park ({park_factor:.2f}x) — {note}."
    return f"Slightly hitter-friendly ({park_factor:.2f}x)."


def _bvp_verdict(bvp: dict, player_name: str, pitcher_name: str, is_under: bool) -> str:
    """Mirrors build_analyze_embed()'s BvP plain-language verdict tiers exactly
    (backend/analyze.py ~line 1838) -- an 8+ AB history earns strong
    "owns/dominates" language, 3-7 AB only earns a soft "small-sample" hedge."""
    ab = bvp.get("ab", 0) or 0
    hits = bvp.get("hits", 0)
    avg_str = bvp.get("avg", ".---")
    try:
        avg = float("0" + avg_str) if str(avg_str).startswith(".") else float(avg_str)
    except (ValueError, TypeError):
        avg = 0.0

    p_first = (player_name or "Batter").split()[0]
    pit_last = (pitcher_name or "pitcher").split()[-1]
    big_sample = ab >= 8

    if not is_under:
        if avg >= 0.380:
            return f"{p_first} owns {pit_last} — big boost for the Over." if big_sample else \
                   f"Small-sample edge — {p_first} is {hits}/{ab} vs {pit_last}; a mild positive lean."
        if avg >= 0.300:
            return f"{p_first} hits {pit_last} well — leans Over." if big_sample else \
                   f"Slight positive ({hits}/{ab}) — too small to weight heavily."
        if avg >= 0.240:
            return "Neutral matchup — history doesn't strongly favor either side."
        if avg >= 0.180:
            return f"{pit_last} has the edge on {p_first} — leans Under." if big_sample else \
                   f"Slightly negative ({hits}/{ab}) — small sample, minor signal."
        return f"{pit_last} dominates {p_first} — big boost for the Under." if big_sample else \
               f"Cold in a small sample ({hits}/{ab}) — minor negative."
    else:
        if avg >= 0.380:
            return f"{p_first} owns {pit_last} — this hurts the Under." if big_sample else \
                   f"Small-sample risk — {p_first} is {hits}/{ab} vs {pit_last}."
        if avg >= 0.300:
            return f"{p_first} has hit {pit_last} a bit — minor risk for the Under."
        if avg >= 0.240:
            return "Neutral matchup — history doesn't strongly favor either side."
        if avg >= 0.180:
            return f"{pit_last} has the edge on {p_first} — supports the Under." if big_sample else \
                   f"Slight Under lean ({hits}/{ab}) — small sample."
        return f"{pit_last} dominates {p_first} — big boost for the Under." if big_sample else \
               f"Cold in a small sample ({hits}/{ab}) — modest Under support."


def _short_date(iso_date: str) -> str:
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(iso_date, "%Y-%m-%d")
        return f"{d.month}/{d.day}"
    except (ValueError, TypeError):
        return ""


def _to_float(val):
    try:
        return round(float(val), 3)
    except (TypeError, ValueError):
        return None


def _damage_label(damage_score) -> str:
    try:
        d = float(damage_score)
    except (TypeError, ValueError):
        return "—"
    if d >= 0.75:
        return "💥 High"
    if d >= 0.4:
        return "Medium"
    return "Low"


def _unit_size_for(tier_label: str) -> str:
    return {
        "Elite": "1.0u", "Strong": "0.75u", "Good": "0.5u",
        "Lean": "0.25u", "Risky": "0u", "Fade": "0u",
    }.get(tier_label, "0.25u")


def _scale(value, lo, hi, out_lo=0, out_hi=10):
    """Clamps value to [lo, hi] and linearly rescales it to [out_lo, out_hi]."""
    if value is None:
        return None
    v = max(lo, min(hi, value))
    return round(out_lo + (v - lo) / (hi - lo) * (out_hi - out_lo), 1)


def _build_confidence_breakdown(picked_grade: dict) -> list:
    """
    Surfaces grade_pick()'s own sub-scores as a 0-10 breakdown instead of
    just the final total. Every entry here maps to a real component the
    model already computed (backend/analyze.py) -- nothing here is invented
    after the fact, and factors with no signal for this prop are omitted
    rather than shown as a fake neutral 5.
    """
    items = []

    proj_edge = picked_grade.get("proj_edge")
    if proj_edge is not None:
        items.append({"label": "Projection Edge", "score": _scale(proj_edge, -3, 3)})

    stability = (picked_grade.get("stability_tier") or "").upper()
    stability_score = {"VOLATILE": 2, "LOW": 4.5, "MEDIUM": 7, "HIGH": 9, "STABLE": 9}.get(stability)
    if stability_score is not None:
        items.append({"label": "Consistency", "score": stability_score})

    damage = picked_grade.get("damage_score")
    if damage:
        # damage_score is additive-only in grade_pick (0..+6), unlike the
        # signed sub-scores below -- scale from its real range so a middling
        # +2 reads as 3.3/10, not a fake near-perfect mark.
        items.append({"label": "Power Profile", "score": _scale(damage, 0, 6)})

    discipline = picked_grade.get("discipline_score")
    if discipline:
        items.append({"label": "Plate Discipline", "score": _scale(discipline, -2, 2)})

    ump = picked_grade.get("ump_score")
    if ump:
        items.append({"label": "Umpire Zone", "score": _scale(ump, -1, 1)})

    pitch_mix = picked_grade.get("pitch_mix_score")
    if pitch_mix:
        items.append({"label": "Pitch Mix", "score": _scale(pitch_mix, -2, 2)})

    hand_ops = picked_grade.get("hand_ops_score")
    if hand_ops:
        items.append({"label": "Handedness Split", "score": _scale(hand_ops, -3, 3)})

    lineup_spot = picked_grade.get("lineup_spot")
    if lineup_spot:
        items.append({"label": "Lineup Volume", "score": _scale(9 - lineup_spot, 0, 8)})

    return items


def _fair_odds(pct: float):
    """
    Converts a percentage (0-100) into American odds — the price at which a
    bet at that probability breaks even. Purely sample-implied (from the
    real game-log distribution), NOT a sportsbook line; extremes where the
    sample is unanimous (0%/100%) have no meaningful price, so return None.
    """
    if pct is None or pct <= 0 or pct >= 100:
        return None
    p = pct / 100
    if p >= 0.5:
        return f"-{round(100 * p / (1 - p))}"
    return f"+{round(100 * (1 - p) / p)}"


def _format_arsenal(arsenal: list) -> list:
    """Pitch mix straight from stats_mlb.get_pitcher_arsenal -- name, usage %, avg mph."""
    out = []
    for p in (arsenal or []):
        name, pct = p.get("pitch_name"), p.get("pct")
        if not name or pct is None:
            continue
        out.append({
            "name": name,
            "pct": pct,
            "speed": round(p["avg_speed"], 1) if p.get("avg_speed") is not None else None,
        })
    return out


def _umpire_line(umpire: dict) -> str:
    """Home-plate ump + K-zone tendency when umpscorecards data resolved."""
    if not umpire or not umpire.get("name"):
        return ""
    kb = umpire.get("k_boost")
    if kb is None:
        return f"Umpire: {umpire['name']}."
    if kb >= 1:
        return f"Umpire: {umpire['name']} — bigger strike zone (+{kb}% K rate vs avg)."
    if kb <= -1:
        return f"Umpire: {umpire['name']} — tighter strike zone ({kb}% K rate vs avg)."
    return f"Umpire: {umpire['name']} — neutral zone."


def _build_distribution(values: list, line: float, strict_over: bool = False) -> dict:
    """
    Buckets REAL per-game outcomes (from stats_mlb's game log, not a
    simulation) into a frequency histogram, plus the actual over/under
    split at this line computed directly from that same sample.
    strict_over=True uses `v > line` (pitcher K props); default `v >= line`
    matches how the rest of the app defines a hit (backend/stats_mlb.py).
    """
    if not values:
        return None
    ints = [int(v) for v in values]
    n = len(ints)

    # Center the 4-value window on the line rather than always starting at 0,
    # so a 5.5 K prop shows 3/4/5/6 with tails instead of dumping everything
    # into one giant "4+" bucket. Low lines (0.5/1.5) still start at 0.
    low = max(0, int(line) - 2)
    buckets = []
    if low > 0:
        below = sum(1 for v in ints if v < low)
        buckets.append({"value": f"≤{low - 1}", "pct": round(below / n * 100, 1)})
    for i in range(low, low + 4):
        count = sum(1 for v in ints if v == i)
        buckets.append({"value": str(i), "pct": round(count / n * 100, 1)})
    plus = sum(1 for v in ints if v >= low + 4)
    if plus:
        buckets.append({"value": f"{low + 4}+", "pct": round(plus / n * 100, 1)})

    over_n = sum(1 for v in values if (v > line if strict_over else v >= line))
    over_pct = round(over_n / n * 100, 1)
    under_pct = round(100 - over_pct, 1)
    return {
        "buckets": buckets,
        "gamesSampled": n,
        "overPct": over_pct,
        "underPct": under_pct,
        "fairOverOdds": _fair_odds(over_pct),
        "fairUnderOdds": _fair_odds(under_pct),
    }


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 4:
        name, stat, line_arg, side_arg = args
        out = compute_prediction(name, STAT_LABEL_TO_PROP_TYPE[stat], stat, float(line_arg), side_arg)
        print(json.dumps(out, indent=2))
    else:
        print("Usage: python prediction_core.py <player> <stat label> <line> <over|under>")
