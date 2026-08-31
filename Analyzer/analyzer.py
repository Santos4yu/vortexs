"""Standalone MLB prop analyzer powered by the VORTEX stats engine."""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import logging
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

try:
    import analyst_brain  # noqa: E402
    import analyze as scoring  # noqa: E402
    import pitcher_brain  # noqa: E402
    import stats_mlb  # noqa: E402
    import stats_mlb_enrichment as context_data  # noqa: E402
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"Analyzer dependency '{exc.name}' is missing. "
        "Double-click run_analyzer.bat for automatic first-time setup."
    ) from None

# Keep the standalone report clean; backend request diagnostics remain available
# when a real warning/error occurs.
logging.getLogger("vortex.stats_mlb").setLevel(logging.WARNING)


ALIASES = {
    "h": "hits", "hit": "hits", "hits": "hits",
    "tb": "total_bases", "total bases": "total_bases",
    "hr": "home_runs", "home run": "home_runs", "home runs": "home_runs",
    "rbi": "rbis", "rbis": "rbis",
    "runs": "runs_scored", "runs scored": "runs_scored",
    "k": "strikeouts", "ks": "strikeouts", "strikeouts": "strikeouts",
    "walk": "walks", "walks": "walks", "bb": "walks",
    "hrr": "hits_runs_rbis", "h+r+rbi": "hits_runs_rbis",
    "hits+runs+rbis": "hits_runs_rbis",
    "fs": "fantasy_score", "fantasy": "fantasy_score",
    "fantasy score": "fantasy_score", "hitter fantasy score": "fantasy_score",
}


def _clean_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in text).split())


def _name_score(query: str, candidate: str) -> float:
    q, c = _clean_name(query), _clean_name(candidate)
    if not q or not c: return 0.0
    if q == c: return 1.0
    ratio = difflib.SequenceMatcher(None, q, c).ratio()
    q_parts, c_parts = q.split(), c.split()
    if len(q_parts) >= 2 and len(c_parts) >= 2:
        first = difflib.SequenceMatcher(None, q_parts[0], c_parts[0]).ratio()
        last = difflib.SequenceMatcher(None, q_parts[-1], c_parts[-1]).ratio()
        ratio = max(ratio, first * .4 + last * .6)
    return ratio


def _resolve_analyzer_player(name: str) -> int | None:
    """Fuzzy match while prioritizing players attached to an upcoming MLB game."""
    from vortextime import vortex_board_day, vortex_day, vortex_day_offset

    scheduled: dict[int, str] = {}
    for date in (vortex_board_day(), vortex_day(), vortex_day_offset(1), vortex_day_offset(2)):
        for game in stats_mlb.get_todays_schedule(game_date=date).values():
            for key in ("home", "away"):
                pid, pname = game.get(f"{key}_pitcher_id"), game.get(f"{key}_pitcher")
                if pid and pname: scheduled[int(pid)] = pname
    if scheduled:
        pid, score = max(((pid, _name_score(name, pname)) for pid, pname in scheduled.items()),
                         key=lambda item: item[1])
        if score >= .78:
            return pid

    data = stats_mlb._get("/sports/1/players", {
        "season": stats_mlb.SEASON, "hydrate": "currentTeam"
    }, cache_key=f"analyzer_mlb_players_{stats_mlb.SEASON}") or {}
    candidates = [(int(p["id"]), p.get("fullName", "")) for p in data.get("people", []) if p.get("id")]
    if candidates:
        pid, score = max(((pid, _name_score(name, pname)) for pid, pname in candidates),
                         key=lambda item: item[1])
        if score >= .72:
            return pid
    return stats_mlb.get_player_id(name)


def _get_batter_pitch_profile(batter_id: int) -> list[dict]:
    """Current-season Savant AVG/SLG/wOBA by pitch type, cached for 24 hours."""
    season = stats_mlb.SEASON
    cache_dir = Path(__file__).resolve().parent / "cache"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"batter_pitch_profile_{batter_id}_{season}.json"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400:
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    try:
        response = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
            params={"type": "batter", "pitchType": "", "year": season,
                    "team": "", "min": 1, "csv": "true"},
            headers={"User-Agent": "VortexLocalAnalyzer/1.0"}, timeout=25,
        )
        response.raise_for_status()
        rows = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
        result = []
        for row in rows:
            if str(row.get("player_id")) != str(batter_id):
                continue
            pa = int(row.get("pa", 0) or 0)
            if pa < 3:
                continue
            result.append({
                "pitch_type": row.get("pitch_type", ""),
                "pitch_name": row.get("pitch_name", row.get("pitch_type", "")),
                "pa": pa, "ab": pa, "avg": row.get("ba", "n/a"),
                "slg": row.get("slg", "n/a"), "woba": row.get("woba", "n/a"),
                "pitches": int(row.get("pitches", 0) or 0),
                "whiff_pct": row.get("whiff_percent"),
                "data_source": "season_pitch_type",
                "sample_note": f"{season} batter results vs this pitch type ({pa} PA)",
            })
        result.sort(key=lambda row: row["pa"], reverse=True)
        if result:
            cache_file.write_text(json.dumps(result), encoding="utf-8")
        return result
    except (requests.RequestException, OSError, ValueError):
        return []


def _get_pitcher_pitch_profile(pitcher_id: int) -> list[dict]:
    """Official Savant season arsenal with whiff and putaway rates."""
    season = stats_mlb.SEASON
    cache_dir = Path(__file__).resolve().parent / "cache"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"pitcher_pitch_profile_{pitcher_id}_{season}.json"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 43200:
        try: return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): pass
    try:
        response = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
            params={"type": "pitcher", "pitchType": "", "year": season,
                    "team": "", "min": 1, "csv": "true"},
            headers={"User-Agent": "VortexLocalAnalyzer/1.0"}, timeout=25,
        )
        response.raise_for_status()
        rows = []
        for row in csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))):
            if str(row.get("player_id")) != str(pitcher_id): continue
            rows.append({
                "pitch_type": row.get("pitch_type", ""), "pitch_name": row.get("pitch_name", ""),
                "pitch_usage": _num(row.get("pitch_usage")), "pitches": int(_num(row.get("pitches"))),
                "whiff_percent": _num(row.get("whiff_percent")),
                "put_away": _num(row.get("put_away")), "k_percent": _num(row.get("k_percent")),
            })
        rows.sort(key=lambda row: row["pitch_usage"], reverse=True)
        if rows: cache_file.write_text(json.dumps(rows), encoding="utf-8")
        return rows
    except (requests.RequestException, OSError, ValueError):
        return []


def _lineup_pitch_profile(team_id: int) -> tuple[dict, list]:
    lineup = stats_mlb.get_team_lineup(team_id)
    if len(lineup) < 9:
        return {}, []
    totals: dict[str, dict] = {}
    for hitter in lineup:
        for row in stats_mlb.get_batter_arsenal_stats(hitter["id"]):
            pitch_type, sample = row.get("pitch_type"), _num(row.get("pitches"))
            if not pitch_type or sample <= 0: continue
            item = totals.setdefault(pitch_type, {"sample": 0, "whiff": 0, "k": 0})
            item["sample"] += sample
            item["whiff"] += _num(row.get("whiff_pct")) * sample
            item["k"] += _num(row.get("k_pct")) * sample
    result = {pitch: {"whiff_pct": item["whiff"] / item["sample"],
                      "k_pct": item["k"] / item["sample"], "sample": item["sample"]}
              for pitch, item in totals.items() if item["sample"]}
    return result, lineup


def _pitcher_vs_team_history(pitcher_id: int, opp_team_id: int, opponent_name: str,
                             game_log: list) -> dict:
    data = stats_mlb._get(
        f"/people/{pitcher_id}/stats",
        {"stats": "vsTeam", "group": "pitching", "opposingTeamId": opp_team_id, "sportId": 1},
        cache_key=f"analyzer_pitcher_vs_team_{pitcher_id}_{opp_team_id}",
    ) or {}
    rows = (data.get("stats") or [{}])[0].get("splits", [])
    # MLB can return more than one split row here (for example, separate team/
    # season records).  Aggregate the rows instead of silently reading only the
    # first one.  Never report strikeouts without corresponding innings because
    # that is an incomplete/malformed career split, not usable evidence.
    career_ip = career_k = career_er = 0.0
    for row in rows:
        stat = row.get("stat") or {}
        career_ip += stats_mlb._ip_to_float(str(stat.get("inningsPitched", "0")))
        career_k += _num(stat.get("strikeOuts"))
        career_er += _num(stat.get("earnedRuns"))
    career_valid = career_ip > 0
    current_values = [float(g.get("value", 0)) for g in game_log
                      if _clean_name(g.get("opponent", "")) == _clean_name(opponent_name)]
    return {
        "career_ip": round(career_ip, 1) if career_valid else 0,
        "career_k": int(career_k) if career_valid else 0,
        "career_era": round(career_er * 9 / career_ip, 2) if career_valid else None,
        "career_valid": career_valid,
        "current_values": current_values,
    }


def _lineup_bvp_k(lineup: list, pitcher_id: int) -> dict:
    if len(lineup) != 9:
        return {}
    ab = strikeouts = 0
    for hitter in lineup:
        history = stats_mlb.get_bvp_history(hitter["id"], pitcher_id)
        ab += int(history.get("ab", 0) or 0)
        strikeouts += int(history.get("k", 0) or 0)
    return {"ab": ab, "k": strikeouts,
            "k_pct_ab": round(strikeouts / ab * 100, 1) if ab else None}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(rate: Any, side: str) -> str:
    if rate is None:
        return "n/a"
    effective = 100 - _num(rate) if side == "under" else _num(rate)
    return f"{effective:.0f}%"


def _matchup_for_player(player_id: int) -> dict:
    matchup = scoring.get_matchup_info(player_id)
    if not matchup:
        raise RuntimeError("No upcoming MLB matchup was found for this player.")
    return matchup


def _game_environment(matchup: dict) -> tuple[dict, dict, dict]:
    """Resolve the home park and game-time weather for the matched game."""
    # MLB's schedule date is the local slate date, which can differ from the
    # UTC calendar date for evening games. Preserve the date used to discover
    # the matchup instead of deriving it from the UTC timestamp.
    game_date = matchup.get("game_date") or str(matchup.get("game_utc", ""))[:10] or None
    game = {}
    home_id = matchup.get("home_team_id")
    opponent_id = matchup.get("opp_team_id")
    # Match both clubs, not merely one possibly missing ID. This prevents an
    # unrelated game's park/weather from being attached to the prop.
    target_game_pk = matchup.get("game_pk")
    for item in stats_mlb.get_todays_schedule(game_date=game_date).values():
        if target_game_pk is not None and item.get("gamePk") == target_game_pk:
            game = item
            break
        # When MLB supplied a unique game ID, never fall through to a merely
        # similar team matchup on another date or in a doubleheader.
        if target_game_pk is not None:
            continue
        expected_away = opponent_id if matchup.get("is_home") else None
        expected_home_opponent = opponent_id if matchup.get("is_home") is False else None
        ids_valid = home_id is not None and opponent_id is not None
        teams_match = (
            item.get("home_team_id") == home_id
            and (expected_away is None or item.get("away_team_id") == expected_away)
            and (expected_home_opponent is None or item.get("home_team_id") == expected_home_opponent)
        )
        if ids_valid and teams_match:
            game = item
            break
    home_name = game.get("home_team_name") if game else None
    # The enrichment helper uses partial matching; only call it with a verified,
    # non-empty team name and require the returned team to match exactly.
    park = context_data.get_park_factor(home_name) if home_name else {}
    if park and _clean_name(park.get("team", "")) != _clean_name(home_name):
        park = {}
    if park:
        park["source"] = "static multi-year estimate"
        park["source_reliability"] = 0.5
    weather = stats_mlb.get_game_weather(
        game.get("home_abbr", ""), game.get("game_utc", "")
    ) if game else {}
    return game, park, weather


def _beginner_writeup(result: dict) -> str:
    """Explain the strongest reasons for and against the bet in plain English."""
    bet, confidence = result["bet"], result["confidence"]
    factors = [f for f in result.get("analyst_evidence", []) if f.get("reliability", 0) > 0]
    strength = lambda f: (f.get("score", 50) - 50) * f.get("weight", 0) * f.get("reliability", 0)
    positives = sorted((f for f in factors if f.get("score", 50) > 55), key=strength, reverse=True)
    negatives = sorted((f for f in factors if f.get("score", 50) < 45), key=strength)

    def simple(factor: dict, favorable: bool) -> str:
        key, detail = factor.get("key"), factor.get("detail", "")
        side = bet["side"].upper()
        if key == "baseline":
            return "his recent and season production supports this side" if favorable else "his recent and season production does not clear this line consistently"
        if key == "hand":
            return (f"his results against this pitcher's throwing hand support the {side}"
                    if favorable else f"his handedness split works against the {side}")
        if key == "pitcher":
            if bet["side"] == "under":
                return (f"the starter has some ability to limit hitters ({detail})"
                        if favorable else f"the starter is vulnerable enough to threaten the UNDER ({detail})")
            return (f"the starting pitcher is a favorable target ({detail})"
                    if favorable else f"the starting pitcher is a difficult matchup ({detail})")
        if key == "pitch_mix":
            return ("his numbers match up well with the pitches he should see most"
                    if favorable else "he has struggled against the pitches he should see most")
        if key == "bvp":
            return (f"his career history against this pitcher supports the bet ({detail})"
                    if favorable else f"his career history against this pitcher is a warning sign ({detail})")
        if key == "recent_form":
            return "his recent results support this side" if favorable else "his recent results have not supported this side"
        if key == "contact":
            return "his quality of contact supports this market" if favorable else "his contact quality is a poor fit for this market"
        if key == "role":
            return "his lineup position gives him strong opportunity" if favorable else "his lineup position limits his opportunities"
        if key == "team":
            return "his team context creates extra scoring opportunities" if favorable else "his team context limits scoring opportunities"
        if key == "bullpen":
            return "the opposing bullpen is favorable" if favorable else "the opposing bullpen is a difficult late-game matchup"
        if key == "environment":
            return "the park and weather help this side" if favorable else "the park and weather work against this side"
        return detail

    text = (f"This is an {confidence['rating'].upper()}-rated {bet['side'].upper()} with a "
            f"{confidence['score']}/100 confidence score.")
    consensus = result.get("analyst_consensus") or {}
    if consensus.get("usable"):
        text += (f" The model combined {consensus['usable']} reliable areas: "
                 f"{consensus['supporting']} support the bet, {consensus['opposing']} oppose it, "
                 f"and {consensus['neutral']} are neutral.")
    if positives:
        text += f" The main reason to like it is that {simple(positives[0], True)}."
        if len(positives) > 1:
            text += f" It also gets help because {simple(positives[1], True)}."
    else:
        text += " There is no major matchup advantage supporting this side."
    if negatives:
        text += f" The biggest concern is that {simple(negatives[0], False)}."
        if len(negatives) > 1:
            text += f" Another concern is that {simple(negatives[1], False)}."
    if confidence["score"] >= 75:
        text += " The data shows a strong enough edge to consider the bet."
    elif confidence["score"] < 60:
        text += " The edge is not strong enough, so the safer choice is to pass."
    else:
        text += " The signals are mixed, so this is only a cautious lean rather than a strong play."
    return text


def _analyze_pitcher_strikeouts(player: str, player_id: int, line: float, side: str) -> dict:
    matchup = _matchup_for_player(player_id)
    card = stats_mlb.get_pitcher_k_card(
        player, line, opp_team_id=matchup.get("opp_team_id"),
        pitcher_id=player_id, prop_type="strikeouts",
    )
    if card.get("error"):
        raise RuntimeError(card["error"])
    game, park, weather = _game_environment(matchup)
    metrics = stats_mlb.get_pitcher_metrics(player, pitcher_id=player_id)
    pitch_profile = _get_pitcher_pitch_profile(player_id)
    lineup_profile, lineup = _lineup_pitch_profile(matchup.get("opp_team_id"))
    team_history = _pitcher_vs_team_history(
        player_id, matchup.get("opp_team_id"), matchup.get("opponent", ""),
        (card.get("splits") or {}).get("game_log", []),
    )
    lineup_bvp = _lineup_bvp_k(lineup, player_id)
    opponent_profile = stats_mlb.get_team_offensive_profile(matchup.get("opp_team_id"))
    brain = pitcher_brain.evaluate(
        side=side, line=line, card=card, pitcher_metrics=metrics,
        pitcher_pitches=pitch_profile, lineup_pitch_profile=lineup_profile,
        lineup_confirmed=len(lineup) == 9, opponent_profile=opponent_profile,
        park=park, weather=weather, team_history=team_history,
        pitcher_is_home=matchup.get("is_home"), lineup_bvp=lineup_bvp,
    )
    return {
        "analysis_type": "pitcher_strikeouts",
        "bet": {"player": card["pitcher_name"], "side": side, "line": line,
                "prop_type": "pitcher_strikeouts", "prop_label": "Pitcher Strikeouts"},
        "matchup": matchup, "confidence": {"score": brain["score"], "rating": brain["label"]},
        "analyst_coverage": brain["coverage"], "analyst_conflicts": brain["conflicts"],
        "analyst_consensus": brain["consensus"],
        "analyst_evidence": brain["evidence"], "card": card, "pitcher_metrics": metrics,
        "pitcher_pitch_profile": pitch_profile, "lineup_pitch_profile": lineup_profile,
        "lineup": lineup, "opponent_profile": opponent_profile,
        "team_history": team_history, "lineup_bvp": lineup_bvp,
        "game": game, "park": park, "weather": weather,
    }


def analyze_prop(player: str, prop: str, line: float, side: str,
                 pitcher_override: str | None = None) -> dict:
    prop_type = ALIASES.get(prop.strip().lower())
    if not prop_type:
        valid = ", ".join(sorted(set(ALIASES.values())))
        raise ValueError(f"Unknown prop '{prop}'. Valid markets: {valid}")
    side = side.lower()
    if side not in {"over", "under"}:
        raise ValueError("Side must be over or under.")

    player_id = _resolve_analyzer_player(player)
    if not player_id:
        raise RuntimeError(f"MLB player not found: {player}")
    profile = stats_mlb._get_player_profile(player_id)
    resolved_name = profile.get("fullName") or player
    position = (profile.get("primaryPosition") or {}).get("abbreviation", "")
    if prop_type == "strikeouts" and position == "P":
        return _analyze_pitcher_strikeouts(resolved_name, player_id, line, side)
    matchup = _matchup_for_player(player_id)
    pitcher_name = pitcher_override or matchup.get("pitcher")
    if not pitcher_name:
        raise RuntimeError("The opposing probable pitcher is still TBD. Use --pitcher to supply one.")

    card = stats_mlb.get_full_card(
        resolved_name, pitcher_name, line, prop_type, side=side,
        opp_team_id=matchup.get("opp_team_id"),
    )
    if card.get("error"):
        raise RuntimeError(card["error"])

    pitcher_id = (card.get("pitcher") or {}).get("pitcher_id")
    if pitcher_id and not card.get("bat_vs_pitch"):
        card["bat_vs_pitch"] = _get_batter_pitch_profile(player_id)
    game, park, weather = _game_environment(matchup)
    park_factor = _num(park.get("factor"), 1.0)
    venue = stats_mlb.get_pitcher_venue_splits(pitcher_id) if pitcher_id else {}
    bullpen = stats_mlb.get_bullpen_stats(matchup.get("opp_team_id"))
    lineup_spot = stats_mlb.get_lineup_position(player_id)

    grade = scoring.grade_pick_v2(
        card["splits"], line, side=side,
        pitcher=card.get("pitcher"), bvp=card.get("bvp"),
        oaa=card.get("oaa"), prop_type=prop_type,
        lineup_spot=lineup_spot, statcast=card.get("statcast"),
        arsenal=card.get("arsenal"), bat_vs_pitch=card.get("bat_vs_pitch"),
        vs_hand_splits=card.get("vs_hand_splits"), opp_bullpen=bullpen,
        park_factor=park_factor, weather=weather,
    )
    matchup_grade = scoring._matchup_score_100(
        card["splits"], side=side, pitcher=card.get("pitcher"),
        bvp=card.get("bvp"), park_factor=park_factor, weather=weather,
        arsenal=card.get("arsenal"), bat_vs_pitch=card.get("bat_vs_pitch"),
        vs_hand_splits=card.get("vs_hand_splits"),
    )
    for factor in matchup_grade.get("factors", []):
        if factor.get("key") == "pitcher_quality":
            factor["detail"] = str(factor.get("detail", "")).replace(" FIP", " estimated FIP")
    team_id = stats_mlb.get_player_current_team(player_id)
    team_profile = stats_mlb.get_team_offensive_profile(team_id) if team_id else {}
    brain = analyst_brain.evaluate(
        prop=prop_type, side=side, line=line, splits=card["splits"],
        matchup_factors=matchup_grade["factors"], statcast=card.get("statcast") or {},
        lineup_spot=lineup_spot, team_profile=team_profile, bullpen=bullpen,
        park=park, weather=weather, bvp=card.get("bvp") or {},
        pitch_profile=card.get("bat_vs_pitch") or [], arsenal=card.get("arsenal") or [],
        home_away=card.get("home_away") or {}, batter_is_home=matchup.get("is_home"),
        pitcher_venue=venue, pitcher_season_era=_num((card.get("pitcher") or {}).get("era")),
    )

    return {
        "bet": {"player": card["batter_name"], "side": side, "line": line,
                "prop_type": prop_type, "prop_label": card["prop_label"]},
        "matchup": {**matchup, "pitcher": pitcher_name},
        "confidence": {"score": brain["score"], "rating": brain["label"],
                       "factor_agreement_pct": grade.get("agreement_pct"),
                       "risk_penalty": grade.get("risk_penalty")},
        "analyst_method": brain["method"],
        "analyst_coverage": brain["coverage"],
        "analyst_conflicts": brain["conflicts"],
        "analyst_consensus": brain["consensus"],
        "analyst_evidence": brain["evidence"],
        "categories": grade["categories"],
        "matchup_score": brain["context_score"],
        "matchup_label": brain["context_label"],
        "matchup_coverage": matchup_grade["coverage"],
        "matchup_factors": matchup_grade["factors"],
        "risk_reasons": grade.get("risk_reasons", []),
        "splits": card["splits"], "home_away": card.get("home_away", {}),
        "pitcher": card.get("pitcher", {}), "pitcher_venue": venue,
        "bvp": card.get("bvp", {}), "platoon_note": card.get("platoon_note"),
        "arsenal": card.get("arsenal", []), "bat_vs_pitch": card.get("bat_vs_pitch", []),
        "vs_hand_splits": card.get("vs_hand_splits", {}),
        "statcast": card.get("statcast", {}), "bullpen": bullpen,
        "team_profile": team_profile,
        "lineup_spot": lineup_spot, "game": game, "park": park, "weather": weather,
    }


def _format_pitcher_report_detailed(result: dict) -> str:
    bet, confidence, card = result["bet"], result["confidence"], result["card"]
    side, evidence = bet["side"], result.get("analyst_evidence", [])
    positives = [e for e in evidence if e["score"] >= 56 and e["reliability"] > 0]
    negatives = [e for e in evidence if e["score"] <= 44 and e["reliability"] > 0]
    key_names = {"baseline": "recent strikeout results", "workload": "workload and starter leash",
                 "opponent_k": "the opponent's strikeout tendency", "arsenal_fit": "arsenal whiff matchup",
                 "command": "pitcher command", "opponent_quality": "opponent offensive quality",
                 "environment": "park and weather"}
    verdict = (f"This is an {confidence['rating']}-rated {side.upper()} at {confidence['score']}/100. ")
    consensus = result.get("analyst_consensus") or {}
    if consensus.get("usable"):
        verdict += (f"It combines {consensus['usable']} reliable categories: "
                    f"{consensus['supporting']} support, {consensus['opposing']} oppose, and "
                    f"{consensus['neutral']} are neutral. ")
    if positives: verdict += f"The strongest reason to like it is {key_names[positives[0]['key']]}. "
    if negatives: verdict += f"The biggest concern is {key_names[negatives[0]['key']]}. "
    verdict += ("The edge is strong enough to consider." if confidence["score"] >= 75 else
                "This is a cautious lean, not a top play." if confidence["score"] >= 65 else
                "The safer decision is to pass because the edge is not strong enough.")
    lines = [
        "=" * 72, f"VORTEX PITCHER-K ANALYZER — {bet['player'].upper()}",
        f"BET: {side.upper()} {bet['line']:g} PITCHER STRIKEOUTS",
        f"MATCHUP: {result['matchup'].get('player_team', bet['player'])} vs {result['matchup'].get('opponent', '?')}",
        f"CONFIDENCE: {confidence['score']}/100 — {confidence['rating']}", "=" * 72,
        "\nEASY-TO-READ VERDICT", f"  {verdict}",
        f"\nPITCHER-K ANALYST PROCESS  (data coverage: {result.get('analyst_coverage', 0)*100:.0f}%)",
    ]
    for item in evidence:
        lines.append(f"  {item['key'].replace('_', ' ').title():18} {item['score']-50:+5.1f}  "
                     f"weight {item['weight']:2d} | reliability {item['reliability']*100:3.0f}% | {item['detail']}")
    if consensus.get("usable"):
        lines.append(f"  Overall consensus: {consensus['supporting']} support / {consensus['opposing']} oppose / "
                     f"{consensus['neutral']} neutral | directional agreement {consensus['agreement']*100:.0f}%")
    if result.get("analyst_conflicts"):
        lines.append(f"  Conflict check: {result['analyst_conflicts']} strong signal conflict(s); confidence reduced.")
    splits = card.get("splits") or {}
    lines.append("\nRECENT STRIKEOUT RESULTS")
    for key in ("l5", "l10", "l20"):
        row = splits.get(key) or {}
        rate = row.get("rate")
        effective = (100 - _num(rate)) if side == "under" and rate is not None else rate
        lines.append(f"  {key.upper():3}: {effective if effective is not None else 'n/a'}% for this side | "
                     f"average {row.get('avg', 'n/a')} | {row.get('values', [])}")
    ss = card.get("season_stats") or {}
    venue_key = "home" if result.get("matchup", {}).get("is_home") else "away"
    venue_split = card.get(f"{venue_key}_k_split") or {}
    history = result.get("team_history") or {}
    career_team_text = (
        f"{history.get('career_k', 0)} K in {history.get('career_ip', 0)} IP"
        if history.get("career_valid") else "unavailable (incomplete MLB split)"
    )
    lines += ["\nPITCHER PROFILE",
              f"  ERA {ss.get('era')} | estimated FIP {ss.get('fip')} | WHIP {ss.get('whip')} | "
              f"K/9 {ss.get('k_per_9')} | {ss.get('k_per_gs')} K/start",
              f"  {venue_key.title()} split: {venue_split.get('avg', 'n/a')} K/start | "
              f"{venue_split.get('over_rate', 'n/a')}% Over this line in {venue_split.get('starts', 0)} starts",
              f"  Verified MLB game: {result.get('game', {}).get('gamePk', 'unavailable')} | "
              f"slate date {result.get('matchup', {}).get('game_date', 'unavailable')}",
              f"  Career vs opponent: {career_team_text} | "
              f"current-season starts: {history.get('current_values', [])}",
              "\nARSENAL WHIFF + PUTAWAY"]
    for pitch in result.get("pitcher_pitch_profile", []):
        lines.append(f"  {pitch.get('pitch_name', pitch.get('pitch_type')):16} {pitch.get('pitch_usage', 0):5.1f}% usage | "
                     f"{pitch.get('whiff_percent', 0):4.1f}% whiff | {pitch.get('put_away', 0):4.1f}% putaway")
    opp = result.get("opponent_profile") or {}
    lines += ["\nOPPONENT + LINEUP",
              f"  Team AVG {opp.get('avg', 'n/a')} | K% {opp.get('k_pct', 'n/a')} | BB% {opp.get('bb_pct', 'n/a')} | "
              f"runs/game {opp.get('runs_pg', 'n/a')}",
              f"  Confirmed lineup: {'yes' if len(result.get('lineup', [])) == 9 else 'no — lineup-specific arsenal edge withheld'}",
              (f"  Confirmed-lineup history vs pitcher: {result.get('lineup_bvp', {}).get('k', 0)} K in "
               f"{result.get('lineup_bvp', {}).get('ab', 0)} career AB"
               if result.get("lineup_bvp") else "  Confirmed-lineup history vs pitcher: unavailable"),
              "\nMODEL VERDICT",
              f"  {confidence['rating']} {side.upper()} — {confidence['score']}/100.",
              "  This is analytical information, not a guarantee of a betting result."]
    return "\n".join(lines)


def _format_detailed_report(result: dict) -> str:
    if result.get("analysis_type") == "pitcher_strikeouts":
        return _format_pitcher_report_detailed(result)
    bet, conf = result["bet"], result["confidence"]
    splits, pitcher, bvp = result["splits"], result["pitcher"], result["bvp"]
    side = bet["side"]
    matchup = result["matchup"]
    lines = [
        "=" * 72,
        f"VORTEX MLB PROP ANALYZER — {bet['player'].upper()}",
        f"BET: {side.upper()} {bet['line']:g} {bet['prop_label']}",
        f"MATCHUP: vs {matchup.get('opponent', '?')} | opposing SP: {matchup.get('pitcher', 'TBD')}",
        f"CONFIDENCE: {conf['score']}/100 — {conf['rating'].upper()}",
        f"MATCHUP SCORE: {result.get('matchup_score', 50)}/100 — {result.get('matchup_label', 'Neutral')}",
        "=" * 72,
        "\nEASY-TO-READ VERDICT",
        f"  {_beginner_writeup(result)}",
        f"\nHUMAN-ANALYST PROCESS  (data coverage: {result.get('analyst_coverage', 0)*100:.0f}%)",
    ]
    for item in result.get("analyst_evidence", []):
        lean = item["score"] - 50
        lines.append(f"  {item['key'].replace('_', ' ').title():16} {lean:+5.1f}  "
                     f"weight {item['weight']:2d} | reliability {item['reliability']*100:3.0f}% | {item['detail']}")
    consensus = result.get("analyst_consensus") or {}
    if consensus.get("usable"):
        lines.append(f"  Overall consensus: {consensus['supporting']} support / {consensus['opposing']} oppose / "
                     f"{consensus['neutral']} neutral | directional agreement {consensus['agreement']*100:.0f}%")
    if result.get("analyst_conflicts"):
        lines.append(f"  Conflict check: {result['analyst_conflicts']} strong signal conflict(s); confidence reduced.")

    lines += ["\nSECONDARY MODEL CROSS-CHECK"]
    for name, data in result["categories"].items():
        direction = data["direction"]
        if name == "form" and direction != "neutral":
            direction = side if data["score"] >= 5 else ("under" if side == "over" else "over")
        lines.append(f"  {name.replace('_', ' ').title():12} {data['score']:.1f}/10 "
                     f"(data confidence {data['confidence'] * 100:.0f}%, leans {direction})")

    lines += ["\nRECENT RESULTS"]
    for key in ("l5", "l10", "l20"):
        row = splits.get(key) or {}
        lines.append(f"  {key.upper():3}: {_pct(row.get('rate'), side)} hit rate for this side "
                     f"({row.get('games', 0)} games), average {row.get('avg', 'n/a')}")

    lines += ["\nSTARTING PITCHER"]
    lines.append(f"  {pitcher.get('name', matchup.get('pitcher'))} ({pitcher.get('hand', '?')}HP): "
                 f"ERA {pitcher.get('era', 'n/a')} | estimated FIP {pitcher.get('fip', 'n/a')} | "
                 f"WHIP {pitcher.get('whip', 'n/a')} | K/9 {pitcher.get('k_per_9', 'n/a')} | "
                 f"HR/9 {pitcher.get('hr_per_9', 'n/a')}")
    venue = result.get("pitcher_venue") or {}
    if venue:
        tonight = "home" if not matchup.get("is_home") else "away"
        lines.append(f"  Venue ERA: home {venue.get('home_era', 'n/a')} / away {venue.get('away_era', 'n/a')} "
                     f"— pitcher is {tonight} tonight")

    lines += ["\nPITCH MIX vs BATTER RESULTS"]
    by_pitch = {x.get("pitch_type"): x for x in result.get("bat_vs_pitch", [])}
    arsenal = sorted(result.get("arsenal", []), key=lambda x: _num(x.get("pct")), reverse=True)
    if not arsenal:
        lines.append("  Pitch-level data unavailable (the model treats it as missing, not negative).")
    for pitch in arsenal:
        code = pitch.get("pitch_type", "?")
        bat = by_pitch.get(code, {})
        source = bat.get("sample_note") or "pitch-type sample unavailable"
        lines.append(f"  {pitch.get('pitch_name', code):16} {_num(pitch.get('pct')):5.1f}% usage | "
                     f"batter AVG {bat.get('avg', 'n/a')} | SLG {bat.get('slg', 'n/a')} | "
                     f"wOBA {bat.get('woba', 'n/a')} | {source}")

    lines += ["\nBvP + SPLITS"]
    if bvp.get("ab"):
        lines.append(f"  Career vs pitcher: {bvp.get('hits', 0)}-for-{bvp['ab']}, AVG {bvp.get('avg')}, "
                     f"estimated OPS {bvp.get('ops')}, HR {bvp.get('hr', 0)}, K {bvp.get('k', 0)} ({bvp.get('sample')})")
    else:
        lines.append("  Career vs pitcher: no meaningful history; no BvP boost was applied.")
    ha = result.get("home_away") or {}
    venue_key = "home" if matchup.get("is_home") else "away"
    lines.append(f"  Batter venue: {venue_key} average {ha.get(venue_key + '_avg', 'n/a')} across "
                 f"{ha.get(venue_key + '_games', 0)} games")
    lines.append(f"  Platoon: {result.get('platoon_note')}")

    if result.get("risk_reasons"):
        lines.append("\nRISKS / REASONS TO PASS")
        lines.extend(f"  - {reason}" for reason in result["risk_reasons"])
    lines += ["\nMODEL VERDICT",
              f"  {conf['rating'].upper()} {side.upper()} — {conf['score']}/100. "
              "Missing or small-sample data is confidence-discounted.",
              "  This is analytical information, not a guarantee of a betting result."]
    return "\n".join(lines)


def _decision_text(score: int) -> str:
    if score >= 85: return "Top-rated edge — still verify the lineup before betting."
    if score >= 75: return "Strong enough to consider if the lineup remains favorable."
    if score >= 65: return "Small edge only — use caution."
    return "PASS — the full data set does not show a strong enough edge."


def _compact_reasons(result: dict) -> list[str]:
    evidence = [e for e in result.get("analyst_evidence", []) if e.get("reliability", 0) > 0]
    supports = [e for e in evidence if e.get("score", 50) >= 56][:3]
    risks = [e for e in evidence if e.get("score", 50) <= 44][:2]
    names = {
        "baseline": "Recent + season results", "workload": "Workload / leash",
        "opponent_k": "Opponent strikeout profile", "arsenal_fit": "Pitch arsenal fit",
        "command": "Command", "opponent_quality": "Opponent offense",
        "environment": "Park / weather", "hand": "Handedness split",
        "pitch_mix": "Pitch-mix matchup", "contact": "Contact quality",
        "pitcher": "Starting-pitcher matchup", "role": "Lineup role",
        "team": "Team offense", "bullpen": "Opposing bullpen", "bvp": "Batter vs pitcher",
    }
    def short_detail(e: dict) -> str:
        parts = [part.strip() for part in str(e.get("detail", "")).split(";") if part.strip()]
        limit = 1 if e.get("key") in {"arsenal_fit", "pitch_mix"} else 2
        text = "; ".join(parts[:limit])
        return text if len(text) <= 145 else text[:142].rstrip() + "..."
    lines = []
    if supports:
        lines.append("\nWHAT SUPPORTS THE BET")
        lines.extend(f"  + {names.get(e['key'], e['key'].title())}: {short_detail(e)}" for e in supports)
    if risks:
        lines.append("\nWHAT WORKS AGAINST IT")
        lines.extend(f"  - {names.get(e['key'], e['key'].title())}: {short_detail(e)}" for e in risks)
    if not risks:
        lines.extend(["\nWHAT WORKS AGAINST IT", "  - No major negative signal, but uncertainty still lowers confidence."])
    return lines


def _format_compact_pitcher_report(result: dict) -> str:
    bet, conf, card = result["bet"], result["confidence"], result["card"]
    matchup, side = result["matchup"], bet["side"]
    consensus = result.get("analyst_consensus") or {}
    splits, ss = card.get("splits") or {}, card.get("season_stats") or {}
    venue_key = "home" if matchup.get("is_home") else "away"
    venue = card.get(f"{venue_key}_k_split") or {}
    opp = result.get("opponent_profile") or {}
    lines = [
        "=" * 64,
        f"VORTEX ANALYZER | {bet['player'].upper()}",
        f"PLAY: {side.upper()} {bet['line']:g} PITCHER STRIKEOUTS",
        f"GAME: {matchup.get('player_team', '?')} vs {matchup.get('opponent', '?')}",
        f"GRADE: {conf['score']}/100 | {conf['rating']}",
        f"DECISION: {_decision_text(conf['score'])}",
        "=" * 64,
    ]
    lines += _compact_reasons(result)
    lines += ["\nQUICK STAT CHECK"]
    for key in ("l5", "l10", "l20"):
        row = splits.get(key) or {}
        rate = row.get("rate")
        effective = 100 - _num(rate) if side == "under" and rate is not None else rate
        lines.append(f"  {key.upper()}: cleared {bet['line']:g} in {effective if effective is not None else 'n/a'}% | {row.get('avg', 'n/a')} K average")
    lines += [
        f"  Season: {ss.get('k_per_gs', 'n/a')} K/start | {ss.get('k_per_9', 'n/a')} K/9 | {ss.get('era', 'n/a')} ERA",
        f"  {venue_key.title()}: {venue.get('avg', 'n/a')} K/start in {venue.get('starts', 0)} starts",
        f"  Opponent: {opp.get('k_pct', 'n/a')}% K | {opp.get('bb_pct', 'n/a')}% BB | {opp.get('runs_pg', 'n/a')} runs/game",
        f"  Lineup: {'confirmed and analyzed' if len(result.get('lineup', [])) == 9 else 'not confirmed — lineup-specific credit withheld'}",
        "\nMODEL CHECK",
        f"  Data coverage: {result.get('analyst_coverage', 0)*100:.0f}%",
        f"  Signals: {consensus.get('supporting', 0)} support | {consensus.get('opposing', 0)} oppose | {consensus.get('neutral', 0)} neutral",
        f"  Verified game: MLB {result.get('game', {}).get('gamePk', 'unavailable')}",
        "\nUse --details to show every weight, pitch and diagnostic.",
    ]
    return "\n".join(lines)


def _format_compact_hitter_report(result: dict) -> str:
    bet, conf, matchup = result["bet"], result["confidence"], result["matchup"]
    splits, consensus = result.get("splits") or {}, result.get("analyst_consensus") or {}
    pitcher = result.get("pitcher") or {}
    lines = [
        "=" * 64,
        f"VORTEX ANALYZER | {bet['player'].upper()}",
        f"PLAY: {bet['side'].upper()} {bet['line']:g} {bet['prop_label']}",
        f"GAME: {matchup.get('player_team', bet['player'])} vs {matchup.get('opponent', '?')}",
        f"OPPOSING STARTER: {matchup.get('pitcher', 'TBD')}",
        f"GRADE: {conf['score']}/100 | {conf['rating'].upper()}",
        f"DECISION: {_decision_text(conf['score'])}",
        "=" * 64,
        "\nWHY",
        f"  {_beginner_writeup(result)}",
    ]
    lines += _compact_reasons(result)
    lines += ["\nQUICK STAT CHECK"]
    for key in ("l5", "l10", "l20"):
        row = splits.get(key) or {}
        lines.append(f"  {key.upper()}: {_pct(row.get('rate'), bet['side'])} hit rate | {row.get('avg', 'n/a')} average")
    lines += [
        f"  Starter: {pitcher.get('era', 'n/a')} ERA | {pitcher.get('whip', 'n/a')} WHIP | {pitcher.get('hand', '?')}HP",
        f"  Lineup spot: {result.get('lineup_spot') or 'not confirmed'}",
        "\nMODEL CHECK",
        f"  Data coverage: {result.get('analyst_coverage', 0)*100:.0f}%",
        f"  Signals: {consensus.get('supporting', 0)} support | {consensus.get('opposing', 0)} oppose | {consensus.get('neutral', 0)} neutral",
        "\nUse --details to show every weight, pitch and diagnostic.",
    ]
    return "\n".join(lines)


def format_report(result: dict, detailed: bool = False) -> str:
    if detailed:
        return _format_detailed_report(result)
    if result.get("analysis_type") == "pitcher_strikeouts":
        return _format_compact_pitcher_report(result)
    return _format_compact_hitter_report(result)


def _save_analysis_snapshot(result: dict) -> None:
    """Store model inputs locally so weights can later be calibrated to outcomes."""
    db_path = Path(__file__).resolve().parent / "analyzer_history.db"
    try:
        with sqlite3.connect(db_path) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    player TEXT NOT NULL, prop_type TEXT NOT NULL, line REAL NOT NULL,
                    side TEXT NOT NULL, score INTEGER NOT NULL, rating TEXT NOT NULL,
                    coverage REAL, evidence_json TEXT NOT NULL,
                    actual_value REAL, outcome TEXT
                )
            """)
            bet, confidence = result["bet"], result["confidence"]
            con.execute("""
                INSERT INTO analyses
                (player, prop_type, line, side, score, rating, coverage, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (bet["player"], bet["prop_type"], bet["line"], bet["side"],
                  confidence["score"], confidence["rating"], result.get("analyst_coverage"),
                  json.dumps(result.get("analyst_evidence", []))))
    except (sqlite3.Error, OSError):
        pass


def _prompt_missing(args: argparse.Namespace) -> None:
    args.player = args.player or input("MLB player: ").strip()
    args.prop = args.prop or input("Prop (hits, TB, HR, RBI, runs, Ks [pitcher or batter], walks, HRR, FS): ").strip()
    if args.line is None:
        args.line = float(input("Line: ").strip())
    if not args.side:
        side_aliases = {
            "over": "over", "o": "over", "more": "over",
            "under": "under", "u": "under", "less": "under",
        }
        while not args.side:
            raw_side = input("Direction — Over or Under? [O/U]: ").strip().lower()
            args.side = side_aliases.get(raw_side)
            if not args.side:
                print("Please enter Over, Under, O, U, More, or Less.")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Analyze one MLB player prop locally.")
    parser.add_argument("player", nargs="?")
    parser.add_argument("prop", nargs="?")
    parser.add_argument("line", nargs="?", type=float)
    parser.add_argument("side", nargs="?", choices=("over", "under"))
    parser.add_argument("--pitcher", help="Override the probable opposing pitcher")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    parser.add_argument("--details", action="store_true", help="Show every model weight and raw diagnostic")
    args = parser.parse_args()
    try:
        _prompt_missing(args)
        result = analyze_prop(args.player, args.prop, args.line, args.side, args.pitcher)
        _save_analysis_snapshot(result)
        print(json.dumps(result, indent=2, default=str) if args.json else format_report(result, detailed=args.details))
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"Analyzer error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
