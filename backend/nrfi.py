"""
VORTEX — NRFI/YRFI Analysis Engine
====================================
Evaluates every game on today's slate and scores how likely a No-Run First Inning
(NRFI) or Yes-Run First Inning (YRFI) is.

Data sources:
  - MLB Stats API (proxy): pitcher metrics, team hitting, platoon splits
  - Baseball Savant CSVs: Statcast leaderboard (barrel%, hard-hit%)
  - Game linescore API: per-inning scoring for 1st-inning splits

Lineup gate: only produces picks when BOTH sides have top-3 batters confirmed
via the schedule lineup hydrate.  Pitchers come from probablePitcher data.

Public functions
----------------
  get_nrfi_plays(game_date=None)  -> list[dict]
  build_nrfi_embed(plays, date_str) -> discord.Embed
"""

import csv
import io
import json
import logging
from datetime import date as _date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

import stats_mlb as sm
import vortextime
from stats_mlb import log as _mlb_log, CACHE_DIR, SEASON

log = logging.getLogger("vortex.nrfi")


# ── Statcast pitcher leaderboard cache (1 call per day) ───────────────────────

def _load_pitcher_statcast_leaderboard() -> dict[str, dict]:
    """Fetch Savant statcast + expected_stats + plate-discipline for pitchers."""
    today       = _date.today().isoformat()
    cache_key   = f"savant_pitchers_{today}"
    cache_file  = CACHE_DIR / f"{cache_key}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text("utf-8"))
        except Exception:
            pass

    all_data: dict[str, dict] = {}

    # 1. Statcast leaderboard (barrel%, hard-hit%)
    try:
        r = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/statcast",
            params={"type": "pitcher", "year": SEASON, "min": "q", "csv": "true"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=25,
        )
        if r.ok:
            for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
                pid = str(row.get("player_id", "")).strip()
                if not pid:
                    continue
                rec = {}
                rp = row.get("brl_percent")
                if rp:
                    try: rec["barrel_pct"] = float(rp)
                    except: pass
                rh = row.get("ev95percent")
                if rh:
                    try: rec["hard_hit_pct"] = float(rh)
                    except: pass
                if rec:
                    all_data.setdefault(pid, {}).update(rec)
    except Exception as e:
        log.warning("Savant statcast leaderboard failed: %s", e)

    # 2. Expected stats (xERA)
    try:
        r2 = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/expected_statistics",
            params={"type": "pitcher", "year": SEASON, "min": "q", "csv": "true"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=25,
        )
        if r2.ok:
            for row in csv.DictReader(io.StringIO(r2.content.decode("utf-8-sig"))):
                pid = str(row.get("player_id", "")).strip()
                if not pid:
                    continue
                rx = row.get("xera")
                if rx:
                    try:
                        all_data.setdefault(pid, {})["xera"] = float(rx)
                    except: pass
    except Exception as e:
        log.warning("Savant expected_stats failed: %s", e)

    # 3. Plate discipline (whiff%)
    try:
        r3 = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/plate-discipline",
            params={"type": "pitcher", "year": SEASON, "min": "q", "csv": "true"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=25,
        )
        if r3.ok:
            for row in csv.DictReader(io.StringIO(r3.content.decode("utf-8-sig"))):
                pid = str(row.get("player_id", "")).strip()
                if not pid:
                    continue
                rw = row.get("whiff_percent")
                if rw:
                    try:
                        all_data.setdefault(pid, {})["whiff_pct"] = float(rw)
                    except: pass
    except Exception as e:
        log.warning("Savant plate_discipline failed: %s", e)

    if all_data:
        try:
            cache_file.write_text(json.dumps(all_data), encoding="utf-8")
        except Exception:
            pass
    return all_data


# ── Confirmed lineups: top-3 batters ─────────────────────────────────────────

def _get_confirmed_lineup(game_date: str) -> dict[int, dict]:
    """
    Fetch the lineup hydrate for a date and return top-3 batters per game.

    Returns: {game_pk: {"home": [batter_id, batter_id, batter_id],
                        "away": [batter_id, batter_id, batter_id]}}
    Only includes games where both sides have ≥3 hitters listed.
    (Pitchers are not included in the lineup hydrate — only 9 hitters.)
    """
    data = sm._get("/schedule", {
        "sportId": 1, "date": game_date, "gameType": "R",
        "hydrate": "lineups",
    }, cache_key=f"lineups_{game_date}")
    if not data:
        return {}

    result: dict[int, dict] = {}
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            pk = g.get("gamePk")
            if not pk:
                continue
            lineups = g.get("lineups") or {}
            game_info: dict = {"home": [], "away": []}

            for side_key, side_name in [("homePlayers", "home"), ("awayPlayers", "away")]:
                players = lineups.get(side_key) or []
                hitters = [p["id"] for p in players
                           if ((p.get("position") or p.get("primaryPosition") or {})
                               .get("abbreviation", "")) != "P"]
                game_info[side_name] = hitters[:3]

            if len(game_info["home"]) >= 3 and len(game_info["away"]) >= 3:
                result[pk] = game_info

    return result


# ── Batter profile ───────────────────────────────────────────────────────────

def _get_batter_profile(batter_id: int) -> dict:
    """
    Fetch batter's season stats: OPS, K%, BB%, handedness, platoon splits.
    Returns {} on failure.
    """
    profile = sm._get_player_profile(batter_id)
    if not profile:
        return {}
    hand = profile.get("batSide", {}).get("code", "R")
    name = profile.get("fullName", "?")

    # Season hitting stats
    data = sm._get(f"/people/{batter_id}/stats", {
        "stats": "season", "group": "hitting",
        "season": SEASON, "sportId": 1,
    }, cache_key=f"season_bat_{batter_id}_{SEASON}")
    if not data:
        return {"name": name, "hand": hand}

    splits = ((data.get("stats") or [{}])[0]).get("splits", [])
    if not splits:
        return {"name": name, "hand": hand}

    s = splits[0].get("stat", {})
    pa = max(int(s.get("plateAppearances", 1) or 1), 1)
    try:
        ops_f = float(s.get("ops", ".700") or 0.700)
        k_rate = round(int(s.get("strikeOuts", 0)) / pa * 100, 1)
        bb_rate = round(int(s.get("baseOnBalls", 0)) / pa * 100, 1)
        avg_f = float(s.get("avg", ".000") or 0)
    except (ValueError, TypeError):
        ops_f, k_rate, bb_rate, avg_f = 0.700, 22.0, 8.5, 0.250

    return {
        "name":    name,
        "hand":    hand,
        "ops":     s.get("ops", ".---"),
        "ops_f":   ops_f,
        "k_rate":  k_rate,
        "bb_rate": bb_rate,
        "avg":     avg_f,
        "rpg":     round(int(s.get("runs", 0)) / max(int(s.get("gamesPlayed", 1) or 1), 1), 2),
        "pa":      pa,
    }


def _get_platoon_stats(batter_id: int) -> dict:
    """
    Returns batter's splits vs LHP and RHP using existing function.
    Keys: {vs_left: {avg, ops, pa}, vs_right: {avg, ops, pa}}
    """
    splits = sm.get_batter_hand_splits(batter_id)
    out = {}
    for hand, data in splits.items():
        try:
            ops = float(data.get("ops", ".000") or 0)
        except (ValueError, TypeError):
            ops = 0.600
        out[f"vs_{'left' if hand == 'L' else 'right'}"] = {
            "ops":   ops,
            "avg":   data.get("avg", ".000"),
            "pa":    data.get("pa", 0),
        }
    return out


# ── Pitcher 1st-inning data from game log + linescore ──────────────────────

def _get_pitcher_first_inning(pitcher_id: int, pitcher_name: str) -> dict:
    """
    Fetch pitcher's 1st-inning performance by checking linescores for recent starts.
    Returns {first_era, first_er, first_ip, games_sampled} or {} on failure.
    """
    from stats_mlb import get_pitcher_metrics

    pm = get_pitcher_metrics(pitcher_name)
    if "error" in pm:
        return {}

    last_5 = pm.get("last_5_starts", [])
    if not last_5:
        return {}

    first_er = 0
    first_ip = 0.0
    sampled = 0

    for start in last_5[:3]:  # Last 3 starts max
        date_str = start.get("date", "")
        opp_name = start.get("opponent", "")
        if not date_str or not opp_name:
            continue

        # Find game PK for this date + opponent
        opp_team_id = _team_name_to_id(opp_name)
        if not opp_team_id:
            continue

        sched = sm._get("/schedule", {
            "sportId": 1, "date": date_str, "teamId": opp_team_id,
        }, cache_key=f"gamedate_{date_str}_{opp_team_id}")
        if not sched:
            continue

        pk = None
        for dt in sched.get("dates", []):
            for g in dt.get("games", []):
                teams = g.get("teams", {})
                home_id = (teams.get("home", {}).get("team", {}) or {}).get("id")
                away_id = (teams.get("away", {}).get("team", {}) or {}).get("id")
                if opp_team_id in (home_id, away_id):
                    pk = g.get("gamePk")
                    break
        if not pk:
            continue

        # Fetch linescore for this game
        ls = sm._get(f"/game/{pk}/linescore", {},
                     cache_key=f"linescore_{pk}")
        if not ls:
            continue

        innings = ls.get("innings", [])
        if not innings:
            continue

        inn1 = innings[0]
        # Runs allowed by pitcher = runs scored by opponent in 1st inning
        if opp_team_id == home_id:
            # Opponent is home team → opponent's runs = home runs
            runs = inn1.get("home", {}).get("runs", 0)
        else:
            # Opponent is away team → opponent's runs = away runs
            runs = inn1.get("away", {}).get("runs", 0)

        first_er += int(runs or 0)
        first_ip += 1.0  # Each start contributes 1 inning (the 1st)
        sampled += 1

    if sampled == 0:
        return {}

    return {
        "first_era":    round(first_er / sampled * 9, 2),
        "first_er":     first_er,
        "first_ip":     first_ip,
        "games_sampled": sampled,
    }


# ── Team NRFI record (last 15 completed games) ──────────────────────────────

def _get_team_nrfi_record(team_id: int, game_date: str) -> dict:
    """
    Compute a team's NRFI record from their last 15 completed games.
    Returns {total, nrfi_wins, nrfi_losses, pct, l10_nrfi, l10_yrfi, streak_type, streak_n}.
    """
    cache_key = f"team_nrfi_{team_id}_{game_date}"
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text("utf-8"))
        except Exception:
            pass

    try:
        sched = sm._get("/schedule", {
            "sportId": 1, "teamId": team_id, "startDate": f"{game_date}",
            "endDate": f"{game_date}",
            "hydrate": "linescore",
        }, cache_key=f"team_sched_nrfi_{team_id}_{game_date}")
    except Exception:
        return {}

    # Gather completed game PKs for this team (before today)
    game_pks = []
    for dt in (sched or {}).get("dates", []):
        for g in dt.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            pk = g.get("gamePk")
            if pk:
                game_pks.append(pk)

    # If no games on this date, try fetching recent schedule
    if not game_pks:
        try:
            from datetime import datetime, timedelta
            end = datetime.strptime(game_date, "%Y-%m-%d")
            start = end - timedelta(days=20)
            sched2 = sm._get("/schedule", {
                "sportId": 1, "teamId": team_id,
                "startDate": start.strftime("%Y-%m-%d"),
                "endDate": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
            }, cache_key=f"team_sched_recent_{team_id}_{game_date}")
            for dt in (sched2 or {}).get("dates", []):
                for g in dt.get("games", []):
                    if g.get("status", {}).get("abstractGameState") != "Final":
                        continue
                    pk = g.get("gamePk")
                    if pk:
                        game_pks.append(pk)
        except Exception:
            pass

    # Check first inning of last 15 completed games
    nrfi_wins = 0
    nrfi_losses = 0
    results_l15 = []  # True = NRFI, False = YRFI

    for pk in game_pks[:15]:
        try:
            ls = sm._get(f"/game/{pk}/linescore", {},
                         cache_key=f"linescore_{pk}")
            if not ls:
                continue
            innings = ls.get("innings", [])
            if not innings:
                continue
            inn1 = innings[0]
            home_r = int(inn1.get("home", {}).get("runs", 0) or 0)
            away_r = int(inn1.get("away", {}).get("runs", 0) or 0)
            is_nrfi = (home_r + away_r) == 0
            results_l15.append(is_nrfi)
            if is_nrfi:
                nrfi_wins += 1
            else:
                nrfi_losses += 1
        except Exception:
            continue

    total = nrfi_wins + nrfi_losses
    pct = round(nrfi_wins / total * 100) if total else 0

    # L10 from the most recent 10
    l10 = results_l15[:10]
    l10_nrfi = sum(1 for r in l10 if r)
    l10_yrfi = len(l10) - l10_nrfi

    # Current streak
    streak_type = ""
    streak_n = 0
    for r in results_l15:
        if not streak_type:
            streak_type = "NRFI" if r else "YRFI"
            streak_n = 1
        elif (r and streak_type == "NRFI") or (not r and streak_type == "YRFI"):
            streak_n += 1
        else:
            break

    result = {
        "total": total,
        "nrfi_wins": nrfi_wins,
        "nrfi_losses": nrfi_losses,
        "pct": pct,
        "l10_nrfi": l10_nrfi,
        "l10_yrfi": l10_yrfi,
        "streak_type": streak_type,
        "streak_n": streak_n,
    }
    try:
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass
    return result


def _team_name_to_id(name: str) -> Optional[int]:
    """
    Best-effort lookup of a team ID from a team name string.
    The opponent field in game logs uses names like "Cincinnati Reds".
    """
    TEAM_MAP = {
        "Arizona Diamondbacks": 109, "Atlanta Braves": 144,
        "Baltimore Orioles": 110, "Boston Red Sox": 111,
        "Chicago Cubs": 112, "Chicago White Sox": 145,
        "Cincinnati Reds": 113, "Cleveland Guardians": 114,
        "Colorado Rockies": 115, "Detroit Tigers": 116,
        "Houston Astros": 117, "Kansas City Royals": 118,
        "Los Angeles Angels": 108, "Los Angeles Dodgers": 119,
        "Miami Marlins": 146, "Milwaukee Brewers": 158,
        "Minnesota Twins": 142, "New York Mets": 121,
        "New York Yankees": 147, "Oakland Athletics": 133,
        "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134,
        "San Diego Padres": 135, "San Francisco Giants": 137,
        "Seattle Mariners": 136, "St. Louis Cardinals": 138,
        "Tampa Bay Rays": 139, "Texas Rangers": 140,
        "Toronto Blue Jays": 141, "Washington Nationals": 120,
        "Athletics": 133,
    }
    return TEAM_MAP.get(name)


# ── Per-game NRFI/YRFI scorer ────────────────────────────────────────────────

def _score_nrfi_game(game: dict, lh: dict) -> dict:
    """
    Compute NRFI / YRFI scores for a single game.

    game: entry from get_todays_schedule()
    lh:   lineup info from _get_confirmed_lineup()

    Returns dict with recommendation ("NRFI"/"YRFI"/"PASS"),
    confidence, and factor lists.
    """
    pk = game.get("gamePk")

    # Derive game_date from game_utc for team record lookups
    game_date = vortextime.vortex_board_day()
    game_utc = game.get("game_utc", "")
    if game_utc:
        try:
            from datetime import datetime, timezone, timedelta
            game_date = datetime.fromisoformat(
                game_utc.replace("Z", "+00:00")
            ).astimezone(timezone(timedelta(hours=-7))).date().isoformat()
        except Exception:
            pass

    # ── GATE: require top-3 batters per side ──────────────────────────────
    if pk not in lh:
        return {"recommendation": "PASS", "confidence": "PASS",
                "reason": "lineups not confirmed"}

    home_pitcher = game.get("home_pitcher")
    away_pitcher = game.get("away_pitcher")
    home_id      = game.get("home_team_id")
    away_id      = game.get("away_team_id")
    home_name    = game.get("home_team_name", "?")
    away_name    = game.get("away_team_name", "?")
    home_abbr    = game.get("home_abbr", "")
    away_abbr    = game.get("away_abbr", "")

    if not home_pitcher or not away_pitcher:
        return {"recommendation": "PASS", "confidence": "PASS",
                "reason": "missing pitcher"}

    line_info = lh[pk]
    home_batters = line_info.get("home", [])
    away_batters = line_info.get("away", [])

    if len(home_batters) < 3 or len(away_batters) < 3:
        return {"recommendation": "PASS", "confidence": "PASS",
                "reason": "incomplete lineup"}

    # ── Gather pitcher metrics ──────────────────────────────────────────
    hp = sm.get_pitcher_metrics(home_pitcher)
    ap = sm.get_pitcher_metrics(away_pitcher)
    if "error" in hp or "error" in ap:
        return {"recommendation": "PASS", "confidence": "PASS",
                "reason": "pitcher metrics failed"}

    # ── Statcast pitcher data ───────────────────────────────────────────
    lb = _load_pitcher_statcast_leaderboard()
    hp_stat = lb.get(str(hp.get("pitcher_id")), {})
    ap_stat = lb.get(str(ap.get("pitcher_id")), {})

    # ── Team offence context ─────────────────────────────────────────────
    home_off_tm = sm.get_team_hitting_stats(away_id)  # against away pitching
    away_off_tm = sm.get_team_hitting_stats(home_id)   # against home pitching

    # ── Top-3 batter profiles + platoon ──────────────────────────────────
    def _analyze_batters(batter_ids: list[int], pitcher_hand: str, opp_hand: str
                        ) -> tuple[float, list[str], list[str]]:
        """
        Score the top-3 batters for NRFI/YRFI.
        Returns (delta_score, nrfi_reasons, yrfi_reasons).
        Positive delta = batters are weak → good for NRFI.
        """
        score = 0
        nrfi_r: list[str] = []
        yrfi_r: list[str] = []

        total_ops = 0.0
        total_k_rate = 0.0
        total_bb_rate = 0.0
        count = 0

        for bid in batter_ids:
            prof = _get_batter_profile(bid)
            if not prof:
                continue
            count += 1

            ops = prof.get("ops_f", 0.700)
            k_rate = prof.get("k_rate", 22.0)
            bb_rate = prof.get("bb_rate", 8.5)
            b_hand = prof.get("hand", "R")
            total_ops += ops
            total_k_rate += k_rate
            total_bb_rate += bb_rate

            # Platoon advantage check
            p_hand = pitcher_hand  # "R" or "L"
            # The platoon advantage rule: same hand = disadvantage for batter
            if b_hand == p_hand:
                platoon = "same-side"
            else:
                platoon = "cross-side"

            # Get batter's stats vs this pitcher's hand
            platoon_data = _get_platoon_stats(bid)
            vs_key = f"vs_{'right' if p_hand == 'R' else 'left'}"
            vs_stats = platoon_data.get(vs_key, {})
            vs_ops = vs_stats.get("ops", ops)

            # Strong batter signal (bad for NRFI) — dampened to avoid
            # overriding elite pitcher 1st-inning records
            if ops >= 0.850:
                score -= 0.5
                yrfi_r.append(f"{prof['name']} OPS {ops:.3f}")
            elif ops >= 0.780:
                score -= 0.25

            # Weak batter signal (good for NRFI)
            if ops <= 0.650:
                score += 1
                nrfi_r.append(f"{prof['name']} OPS {ops:.3f}")

            # K rate (high K = good for NRFI)
            if k_rate >= 25:
                score += 1
                nrfi_r.append(f"{prof['name']} K% {k_rate}%")
            elif k_rate <= 16:
                score -= 0.5

            # Platoon edge
            if platoon == "cross-side" and vs_ops <= 0.650:
                score += 0.5
            elif platoon == "same-side" and vs_ops >= 0.850:
                score -= 0.5
                yrfi_r.append(f"{prof['name']} vs same-hand {vs_ops:.3f}")

        return score, nrfi_r, yrfi_r

    hp_hand = hp.get("hand", "R")
    ap_hand = ap.get("hand", "R")

    # Home pitcher's top-3 = home_pitcher faces away_batters
    hp_top_score, hp_top_nrfi, hp_top_yrfi = _analyze_batters(
        away_batters, hp_hand, ap_hand)
    # Away pitcher's top-3 = away_pitcher faces home_batters
    ap_top_score, ap_top_nrfi, ap_top_yrfi = _analyze_batters(
        home_batters, ap_hand, hp_hand)

    # ── Pitcher hot/cold (last 3 starts ERA) ──────────────────────────────
    def _hot_cold(last_5: list) -> tuple[int, str, str]:
        """Return (score_delta, trend_label, nrfi_factor/empty)."""
        if not last_5:
            return 0, "neutral", ""
        recent_eras = []
        for g in last_5[:3]:
            ip = g.get("ip", "0.0")
            er = g.get("er", 0)
            try:
                ip_d = float(ip.split(".")[0]) + float(ip.split(".")[1]) / 3
            except Exception:
                ip_d = 0.0
            if ip_d >= 1:
                recent_eras.append(er / ip_d * 9)
        if not recent_eras:
            return 0, "neutral", ""
        avg = sum(recent_eras) / len(recent_eras)
        if avg <= 2.50:
            return 2, "hot", f"L3 ERA {avg:.2f}"
        if avg <= 3.50:
            return 1, "warm", ""
        if avg >= 6.00:
            return -2, "ice cold", f"L3 ERA {avg:.2f}"
        if avg >= 5.00:
            return -1, "cold", ""
        return 0, "neutral", ""

    hp_hot, hp_trend, hp_hot_str = _hot_cold(hp.get("last_5_starts", []))
    ap_hot, ap_trend, ap_hot_str = _hot_cold(ap.get("last_5_starts", []))

    # ── 1st-inning splits ────────────────────────────────────────────────
    hp_fi = _get_pitcher_first_inning(hp.get("pitcher_id"), home_pitcher)
    ap_fi = _get_pitcher_first_inning(ap.get("pitcher_id"), away_pitcher)

    # ── Parse numeric values ─────────────────────────────────────────────
    def _f(val, default=4.5):
        try: return float(val)
        except: return default

    hp_era = _f(hp.get("era"), 4.5)
    hp_k9  = _f(hp.get("k_per_9"), 8.0)
    hp_bb9 = _f(hp.get("bb_per_9"), 3.0)
    hp_whip = _f(hp.get("whip"), 1.35)

    ap_era = _f(ap.get("era"), 4.5)
    ap_k9  = _f(ap.get("k_per_9"), 8.0)
    ap_bb9 = _f(ap.get("bb_per_9"), 3.0)
    ap_whip = _f(ap.get("whip"), 1.35)

    hp_barrel = hp_stat.get("barrel_pct")
    hp_hard   = hp_stat.get("hard_hit_pct")
    ap_barrel = ap_stat.get("barrel_pct")
    ap_hard   = ap_stat.get("hard_hit_pct")

    home_rpg = _f((home_off_tm or {}).get("runs_pg"), 4.5)
    away_rpg = _f((away_off_tm or {}).get("runs_pg"), 4.5)

    pf = sm.PARK_FACTOR.get(home_name, 1.0)

    # ── Weather ──────────────────────────────────────────────────────────
    try:
        weather = sm.get_game_weather(home_abbr, game.get("game_utc", ""))
    except Exception:
        weather = {}

    # ── Umpire ───────────────────────────────────────────────────────────
    ump_tier = None
    try:
        ump_lookup = sm.get_game_umpires()
        ump_name = ump_lookup.get(home_id, "")
        if ump_name:
            ump_tier = sm.UMPIRE_K_TIER.get(ump_name)
    except Exception:
        pass

    # ── Team NRFI records ────────────────────────────────────────────────
    home_nrfi_rec = _get_team_nrfi_record(home_id, game_date) if home_id else {}
    away_nrfi_rec = _get_team_nrfi_record(away_id, game_date) if away_id else {}

    # ── Score each side (weighted model, 0-100 scale) ───────────────────────
    def _nrfi_subscore(
        era: float, k9: float, bb9: float, whip: float,
        barrel: float | None, hard: float | None,
        opp_rpg: float, park: float,
        hot_score: int, hot_str: str,
        fi: dict,
        top_score: float, top_nrfi: list[str], top_yrfi: list[str],
        weather: dict = None, umpire_tier: str = None,
    ) -> tuple[int, list[str], list[str]]:
        """
        Weighted NRFI score (0-100) for one side.
        Higher = more NRFI-favorable.
        Weights: pitcher 1st-inning 30%, pitcher quality 20%, lineup 15%,
                 park 10%, weather 10%, umpire 5%, recent form 10%.
        """
        nrfi_reasons: list[str] = []
        yrfi_reasons: list[str] = []

        # ── 1. Pitcher 1st-inning performance (30 pts max) ────────────────
        fi_score = 15  # neutral
        if fi:
            fier = fi.get("first_era", 9.0)
            fgam = fi.get("games_sampled", 0)
            first_er = fi.get("first_er", 0)
            if first_er == 0 and fgam >= 2:
                fi_score = 30
                nrfi_reasons.append(f"{fgam}/{fgam} clean 1st (100%)")
            elif fier <= 2.00 and fgam >= 2:
                fi_score = 25
                nrfi_reasons.append(f"1st ERA {fier:.2f}")
            elif fier <= 4.00 and fgam >= 2:
                fi_score = 18
            elif fier >= 6.00 and fgam >= 2:
                fi_score = 5
                yrfi_reasons.append(f"1st ERA {fier:.2f}")
            elif fier >= 4.50 and fgam >= 2:
                fi_score = 10

        # ── 2. Pitcher quality (20 pts max) ──────────────────────────────
        quality_score = 10  # neutral
        if era <= 2.50:
            quality_score = 20
            nrfi_reasons.append(f"ERA {era:.2f} (ace)")
        elif era <= 3.50:
            quality_score = 16
            nrfi_reasons.append(f"ERA {era:.2f}")
        elif era <= 4.50:
            quality_score = 12
        elif era >= 5.50:
            quality_score = 4
            yrfi_reasons.append(f"ERA {era:.2f}")

        # K/9 adjustment
        if k9 >= 10.0:
            quality_score = min(20, quality_score + 3)
            nrfi_reasons.append(f"K/9 {k9:.1f}")
        elif k9 >= 8.5:
            quality_score = min(20, quality_score + 1)
        elif k9 < 6.5:
            quality_score = max(0, quality_score - 2)
            yrfi_reasons.append(f"low K/9 {k9:.1f}")

        # BB/9 adjustment
        if bb9 <= 2.5:
            quality_score = min(20, quality_score + 2)
        elif bb9 >= 4.0:
            quality_score = max(0, quality_score - 2)
            yrfi_reasons.append(f"high BB/9 {bb9:.1f}")

        # WHIP adjustment
        if whip <= 1.05:
            quality_score = min(20, quality_score + 1)
        elif whip >= 1.40:
            quality_score = max(0, quality_score - 1)

        # Statcast barrel/hard-hit (pitcher allowed)
        if barrel is not None and barrel >= 9.0:
            quality_score = max(0, quality_score - 2)
            yrfi_reasons.append(f"high barrel {barrel:.1f}%")
        elif barrel is not None and barrel <= 5.0:
            quality_score = min(20, quality_score + 1)
            nrfi_reasons.append(f"brl {barrel:.1f}%")
        if hard is not None and hard >= 40.0:
            quality_score = max(0, quality_score - 1)
            yrfi_reasons.append(f"hard-hit {hard:.1f}%")

        # ── 3. Lineup strength (15 pts max) ──────────────────────────────
        # top_score comes from _analyze_batters: positive = weak lineup (good for NRFI)
        lineup_score = max(0, min(15, 7.5 + top_score * 3))
        nrfi_reasons.extend(top_nrfi)
        yrfi_reasons.extend(top_yrfi)

        # ── 4. Ballpark (10 pts max) ─────────────────────────────────────
        if park <= 0.93:
            park_score = 10
            nrfi_reasons.append(f"pitcher park ({park:.2f})")
        elif park <= 0.97:
            park_score = 8
        elif park <= 1.02:
            park_score = 5
        elif park <= 1.07:
            park_score = 3
            yrfi_reasons.append(f"hitter park ({park:.2f})")
        else:
            park_score = 1
            yrfi_reasons.append(f"hitter park ({park:.2f})")

        # ── 5. Weather (10 pts max) ──────────────────────────────────────
        weather = weather or {}
        if weather.get("dome"):
            weather_score = 5  # neutral for dome
        elif weather.get("error"):
            weather_score = 5
        else:
            speed = weather.get("speed_mph", 0) or 0
            hf = weather.get("hitter_friendly")
            if hf is True and speed >= 10:
                weather_score = 2
                yrfi_reasons.append(f"wind out {speed:.0f} mph")
            elif hf is True:
                weather_score = 4
            elif hf is False and speed >= 10:
                weather_score = 9
                nrfi_reasons.append(f"wind in {speed:.0f} mph")
            elif hf is False:
                weather_score = 7
            else:
                weather_score = 5

        # ── 6. Umpire (5 pts max) ────────────────────────────────────────
        if umpire_tier == "HIGH":
            ump_score = 4  # slightly NRFI (more Ks)
            nrfi_reasons.append("HP ump neutral/high-K")
        elif umpire_tier == "LOW":
            ump_score = 2  # slightly YRFI (smaller zone = more walks/hits)
            yrfi_reasons.append("HP ump small zone")
        else:
            ump_score = 3  # neutral

        # ── 7. Recent form (10 pts max) ──────────────────────────────────
        form_score = 5 + hot_score  # hot_score is -2 to +2
        if hot_str and hot_str != "neutral":
            if hot_score > 0:
                nrfi_reasons.append(f"L3 ERA {hot_str}" if "L3" in hot_str else hot_str)
            elif hot_score < 0:
                yrfi_reasons.append(f"L3 ERA {hot_str}" if "L3" in hot_str else hot_str)

        # Opponent offence adjustment
        if opp_rpg <= 4.0:
            form_score = min(10, form_score + 1)
            nrfi_reasons.append(f"opp O {opp_rpg:.1f}R/G")
        elif opp_rpg >= 5.0:
            form_score = max(0, form_score - 1)

        # ── Total ─────────────────────────────────────────────────────────
        total = fi_score + quality_score + lineup_score + park_score + weather_score + ump_score + form_score
        total = max(0, min(100, total))

        return total, nrfi_reasons, yrfi_reasons

    hp_nrfi, hp_nrfi_f, hp_yrfi_f = _nrfi_subscore(
        hp_era, hp_k9, hp_bb9, hp_whip,
        hp_barrel, hp_hard, away_rpg, pf,
        hp_hot, hp_hot_str, hp_fi,
        hp_top_score, hp_top_nrfi, hp_top_yrfi,
        weather=weather, umpire_tier=ump_tier)
    ap_nrfi, ap_nrfi_f, ap_yrfi_f = _nrfi_subscore(
        ap_era, ap_k9, ap_bb9, ap_whip,
        ap_barrel, ap_hard, home_rpg, pf,
        ap_hot, ap_hot_str, ap_fi,
        ap_top_score, ap_top_nrfi, ap_top_yrfi,
        weather=weather, umpire_tier=ump_tier)

    # Combined score: weighted average (pitchers matter most)
    nrfi_score = round(hp_nrfi * 0.55 + ap_nrfi * 0.45)
    yrfi_score = 100 - nrfi_score

    # This is signal strength, not a calibrated win probability. Select the
    # side first so a YRFI card never receives NRFI's rating.
    if nrfi_score >= 70:
        rec, conf = "NRFI", "STRONG"
        selected_score = nrfi_score
    elif nrfi_score >= 62:
        rec, conf = "NRFI", "LEAN"
        selected_score = nrfi_score
    elif yrfi_score >= 70:
        rec, conf = "YRFI", "STRONG"
        selected_score = yrfi_score
    elif yrfi_score >= 62:
        rec, conf = "YRFI", "LEAN"
        selected_score = yrfi_score
    else:
        rec, conf = "PASS", "PASS"
        selected_score = max(nrfi_score, yrfi_score)

    return {
        "game_pk":        pk,
        "home_team":      home_name,
        "away_team":      away_name,
        "home_abbr":      home_abbr,
        "away_abbr":      away_abbr,
        "home_pitcher":   home_pitcher,
        "away_pitcher":   away_pitcher,
        "game_utc":       game.get("game_utc", ""),
        "nrfi_score":     nrfi_score,
        "yrfi_score":     yrfi_score,
        "recommendation": rec,
        "confidence":     conf,
        "confidence_pct": selected_score,
        "model_rating":  selected_score,
        "nrfi_factors":   hp_nrfi_f + ap_nrfi_f if rec == "NRFI" else [],
        "yrfi_factors":   hp_yrfi_f + ap_yrfi_f if rec == "YRFI" else [],
        "hp_era":         hp_era,
        "ap_era":         ap_era,
        "hp_k9":          hp_k9,
        "ap_k9":          ap_k9,
        "hp_bb9":         hp_bb9,
        "ap_bb9":         ap_bb9,
        "hp_whip":        hp_whip,
        "ap_whip":        ap_whip,
        "hp_barrel":      hp_barrel,
        "ap_barrel":      ap_barrel,
        "hp_hard":        hp_hard,
        "ap_hard":        ap_hard,
        "pf":             pf,
        "home_rpg":       home_rpg,
        "away_rpg":       away_rpg,
        "hp_fi":          hp_fi,
        "ap_fi":          ap_fi,
        "weather":        weather,
        "umpire_tier":    ump_tier,
        "home_nrfi_rec":  home_nrfi_rec,
        "away_nrfi_rec":  away_nrfi_rec,
        "lineup_confirmed": True,
    }


# ── Main entry point ─────────────────────────────────────────────────────────

def get_nrfi_plays(game_date: str = None) -> list[dict]:
    """
    Evaluate all games on today's slate for NRFI/YRFI.
    Only includes games with confirmed pitchers + top-3 batters.
    """
    date_str = game_date or vortextime.vortex_board_day()
    schedule = sm.get_todays_schedule(game_date=date_str)
    if not schedule:
        log.info("No games on schedule for NRFI analysis")
        return []

    log.info("Checking confirmed lineups for %s...", date_str)
    lineup_info = _get_confirmed_lineup(date_str)

    if not lineup_info:
        log.info("No games with confirmed lineups — try dates before/after")
        # Intentionally do not fall back to an earlier slate: that can surface
        # games already in progress when tomorrow's lineups are not ready.

    if not lineup_info:
        log.info("Still no confirmed lineups — no NRFI plays possible")
        return []

    plays = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for pk, game in schedule.items():
        try:
            first_pitch = datetime.fromisoformat((game.get("game_utc") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        # Never return live or completed games from manual /nrfi or automation.
        if first_pitch <= now:
            continue
        try:
            result = _score_nrfi_game(game, lineup_info)
            if result and result.get("recommendation") != "PASS":
                plays.append(result)
        except Exception as e:
            log.warning("NRFI score failed for game %s: %s", pk, e)

    plays.sort(key=lambda p: (
        0 if p.get("recommendation") == "NRFI" else
        1 if p.get("recommendation") == "YRFI" else 2,
        -p.get("nrfi_score", 0) if p.get("recommendation") == "NRFI" else -p.get("yrfi_score", 0),
    ))

    # Log NRFI predictions for grading
    if plays:
        _log_nrfi_predictions(plays, date_str)

    return plays


# ── NRFI prediction logging ──────────────────────────────────────────────────

def _log_nrfi_predictions(plays: list[dict], game_date: str):
    """Save NRFI picks to DB for result grading."""
    import sqlite3
    from pathlib import Path
    from datetime import datetime as _dt, timezone as _tz

    db_path = Path(__file__).resolve().parent.parent / "vortex.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS nrfi_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT, game_date TEXT, game_pk INTEGER,
            home_abbr TEXT, away_abbr TEXT,
            home_pitcher TEXT, away_pitcher TEXT,
            recommendation TEXT, confidence TEXT,
            score INTEGER, confidence_pct REAL,
            result TEXT DEFAULT NULL, actual_result TEXT DEFAULT NULL,
            graded_at TEXT DEFAULT NULL
        )
    """)
    try:
        cur.execute("ALTER TABLE nrfi_predictions ADD COLUMN model_version TEXT DEFAULT 'legacy'")
    except sqlite3.OperationalError:
        pass

    logged_at = _dt.now(_tz.utc).isoformat()
    inserted = 0
    for p in plays:
        pk = p.get("game_pk")
        exists = cur.execute(
            "SELECT 1 FROM nrfi_predictions WHERE game_pk=? AND recommendation=?",
            (pk, p["recommendation"])
        ).fetchone()
        if exists:
            continue

        cur.execute("""
            INSERT INTO nrfi_predictions
              (logged_at, game_date, game_pk, home_abbr, away_abbr,
               home_pitcher, away_pitcher, recommendation, confidence,
               score, confidence_pct, model_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            logged_at, game_date, pk,
            p.get("home_abbr", ""), p.get("away_abbr", ""),
            p.get("home_pitcher", ""), p.get("away_pitcher", ""),
            p["recommendation"], p["confidence"],
            p.get("nrfi_score", 0) if p["recommendation"] == "NRFI" else p.get("yrfi_score", 0),
            p.get("confidence_pct", 0),
            "v2",
        ))
        inserted += 1

    conn.commit()
    conn.close()
    if inserted:
        print(f"  Logged {inserted} NRFI predictions for grading.")


# ── Embed builder (Silas-style) ──────────────────────────────────────────────

def build_nrfi_embed(plays: list[dict], date_str: str):
    """Build a discord.Embed matching Silas's NRFI/YRFI format."""
    import discord
    from datetime import datetime, timezone, timedelta

    active = [p for p in plays if p.get("recommendation") != "PASS"]

    embed = discord.Embed(
        title="🌀 NRFI / YRFI Report",
        description=f"**{date_str}** · {len(active)} plays · lineup-confirmed",
        color=0x9B59B6,
    )

    if not active:
        embed.add_field(
            name="No Plays Today",
            value="No NRFI/YRFI plays meet the confidence threshold or have confirmed lineups.",
            inline=False,
        )
        embed.set_footer(text="Pitchers + top-3 batters required for each side")
        return embed

    for p in active:
        rec  = p.get("recommendation", "PASS")
        conf = p.get("confidence", "PASS")
        h_abbr = p.get("home_abbr", "?")
        a_abbr = p.get("away_abbr", "?")
        hp     = p.get("home_pitcher", "?")
        ap     = p.get("away_pitcher", "?")
        score  = p.get("nrfi_score", 0) if rec == "NRFI" else p.get("yrfi_score", 0)
        conf_pct = p.get("confidence_pct", 0)

        # First pitch countdown
        game_utc = p.get("game_utc", "")
        countdown = ""
        if game_utc:
            try:
                game_dt = datetime.fromisoformat(game_utc.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                diff = game_dt - now
                mins = int(diff.total_seconds() / 60)
                if 0 < mins < 120:
                    countdown = f" · FIRST PITCH IN {mins} MIN"
                elif mins >= 120:
                    h, m = divmod(mins, 60)
                    countdown = f" · FIRST PITCH IN {h}h {m}m"
                game_et = game_dt.astimezone(timezone(timedelta(hours=-4)))
                countdown += f" · {game_et.strftime('%I:%M %p ET').lstrip('0')}"
            except Exception:
                pass

        # Tier icon
        if rec == "NRFI" and conf == "STRONG":
            tier_icon = "🟢"
            badge = f"**{rec}** — Score {score} · Confidence {conf_pct}%"
        elif rec == "NRFI" and conf == "LEAN":
            tier_icon = "🟡"
            badge = f"**{rec}** — Score {score} · Confidence {conf_pct}%"
        elif rec == "YRFI" and conf == "STRONG":
            tier_icon = "🟢"
            badge = f"**{rec}** — Score {100 - score} · Confidence {conf_pct}%"
        elif rec == "YRFI" and conf == "LEAN":
            tier_icon = "🟡"
            badge = f"**{rec}** — Score {100 - score} · Confidence {conf_pct}%"
        else:
            tier_icon = "⚪"
            badge = f"**{rec}**"

        # This is a selected-side model-strength rating, not a probability.
        badge = badge.replace("Confidence", "Model rating").replace("%", "/100")

        # Pitcher stats
        hp_era = p.get("hp_era", 0)
        ap_era = p.get("ap_era", 0)
        hp_k9 = p.get("hp_k9", 0)
        ap_k9 = p.get("ap_k9", 0)

        # 1st-inning splits
        hp_fi = p.get("hp_fi", {})
        ap_fi = p.get("ap_fi", {})

        # Team NRFI records
        home_rec = p.get("home_nrfi_rec", {})
        away_rec = p.get("away_nrfi_rec", {})

        # Build the value for this game
        lines = []

        # Header: matchup + score + countdown
        lines.append(f"{tier_icon} **{badge}**{countdown}")
        lines.append("")

        # Teams — NRFI Record section
        lines.append("**Teams — NRFI Record (scoreless 1st)**")
        if home_rec and home_rec.get("total"):
            h_total = home_rec["total"]
            h_pct = home_rec["pct"]
            h_l10 = home_rec.get("l10_nrfi", 0)
            h_l10_y = home_rec.get("l10_yrfi", 0)
            h_streak = home_rec.get("streak_n", 0)
            h_type = home_rec.get("streak_type", "")
            lines.append(
                f"🏠 {h_abbr}: {home_rec['nrfi_wins']}-{home_rec['nrfi_losses']} "
                f"({h_pct}%) · L10 {h_l10}-{h_l10_y} · "
                f"{h_streak} {h_type}"
            )
        else:
            lines.append(f"🏠 {h_abbr}: no data")

        if away_rec and away_rec.get("total"):
            a_total = away_rec["total"]
            a_pct = away_rec["pct"]
            a_l10 = away_rec.get("l10_nrfi", 0)
            a_l10_y = away_rec.get("l10_yrfi", 0)
            a_streak = away_rec.get("streak_n", 0)
            a_type = away_rec.get("streak_type", "")
            lines.append(
                f"✈️ {a_abbr}: {away_rec['nrfi_wins']}-{away_rec['nrfi_losses']} "
                f"({a_pct}%) · L10 {a_l10}-{a_l10_y} · "
                f"{a_streak} {a_type}"
            )
        else:
            lines.append(f"✈️ {a_abbr}: no data")

        lines.append("")

        # Starting Pitchers
        lines.append("**Starting Pitchers**")
        # Home pitcher
        hp_clean = hp_fi.get("first_er", -1) if hp_fi else -1
        hp_fi_games = hp_fi.get("games_sampled", 0) if hp_fi else 0
        hp_detail = f"🪣 {hp} ({h_abbr})"
        if hp_era:
            hp_detail += f" {hp_era:.2f} ERA"
        if hp_clean == 0 and hp_fi_games >= 2:
            hp_detail += f" · {hp_fi_games}/{hp_fi_games} clean 1st ({round(hp_fi_games/hp_fi_games*100)}%)"
        elif hp_fi and hp_fi_games:
            hp_fi_era = hp_fi.get("first_era", 0)
            hp_detail += f" · {hp_fi_games}G 1st ERA {hp_fi_era:.2f}"
        lines.append(hp_detail)

        # Away pitcher
        ap_clean = ap_fi.get("first_er", -1) if ap_fi else -1
        ap_fi_games = ap_fi.get("games_sampled", 0) if ap_fi else 0
        ap_detail = f"🪣 {ap} ({a_abbr})"
        if ap_era:
            ap_detail += f" {ap_era:.2f} ERA"
        if ap_clean == 0 and ap_fi_games >= 2:
            ap_detail += f" · {ap_fi_games}/{ap_fi_games} clean 1st ({round(ap_fi_games/ap_fi_games*100)}%)"
        elif ap_fi and ap_fi_games:
            ap_fi_era = ap_fi.get("first_era", 0)
            ap_detail += f" · {ap_fi_games}G 1st ERA {ap_fi_era:.2f}"
        lines.append(ap_detail)

        lines.append("")

        # Signals section
        lines.append(f"**{'VORTEX' if rec == 'NRFI' else 'VORTEX'} Signals**")

        # Umpire
        ump_tier = p.get("umpire_tier")
        if ump_tier:
            if ump_tier == "HIGH":
                lines.append(f"✅ HP umpire — high-K zone")
            elif ump_tier == "LOW":
                lines.append(f"⚠️ HP umpire — small zone")
            else:
                lines.append(f"· HP umpire — neutral")
        else:
            lines.append(f"· HP umpire — neutral")

        # Lineup status
        lines.append(f"✅ CONFIRMED lineup")

        # Weather
        weather = p.get("weather", {})
        if weather and not weather.get("error"):
            if weather.get("dome"):
                lines.append(f"🏟️ Indoor — weather N/A")
            else:
                speed = weather.get("speed_mph", 0) or 0
                hf = weather.get("hitter_friendly")
                temp = weather.get("temp_f")
                effect = weather.get("effect", "")
                weather_parts = []
                if speed > 0:
                    weather_parts.append(f"wind {speed:.0f} mph {effect}")
                if temp:
                    weather_parts.append(f"{temp}°F")
                if hf is True:
                    weather_parts.append("hitter-friendly")
                elif hf is False:
                    weather_parts.append("pitcher-friendly")
                if weather_parts:
                    lines.append(f"🌤️ {', '.join(weather_parts)}")

        # Park factor
        pf = p.get("pf", 1.0)
        if pf >= 1.06:
            lines.append(f"🏟️ Hitter park ({pf:.2f})")
        elif pf <= 0.93:
            lines.append(f"🏟️ Pitcher park ({pf:.2f})")

        # Key factors (deduplicated)
        all_factors = p.get("nrfi_factors", []) if rec == "NRFI" else p.get("yrfi_factors", [])
        seen = set()
        unique = []
        for f in all_factors:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        for f in unique[:4]:
            lines.append(f"· {f}")

        # Bottom line
        lines.append("")
        if rec == "NRFI":
            if conf == "STRONG":
                lines.append(f"> *The math says: bet **NRFI** — both starters suppress 1st-inning runs.*")
            else:
                lines.append(f"> *Lean NRFI — watch for lineup confirmations.*")
        elif rec == "YRFI":
            if conf == "STRONG":
                lines.append(f"> *The math says: bet **YRFI** — expect a run in the 1st inning.*")
            else:
                lines.append(f"> *Lean YRFI — vulnerable spots exist but monitor closely.*")

        embed.add_field(
            name=f"{a_abbr} @ {h_abbr}",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"VORTEX · Weighted scoring model · {date_str}")
    return embed
