"""Conservative, bot-only MLB anytime-home-run value model.

The output is a probability estimate and a separate reliability assessment.
Every confirmed hitter with a correctly mapped market is logged, including
PASS candidates, for future grading and calibration.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import stats_mlb as sm
import nrfi
import update_board as ub
import vortextime

DB_PATH = Path(__file__).resolve().parent.parent / "vortex.db"
LEAGUE_HR_PER_PA = 0.030
ODDS_MAX_AGE_MIN = 90
MIN_BOOKS = 1


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))


def _american_fair(prob: float) -> int:
    prob = _clamp(prob, .001, .999)
    return round(-100 * prob / (1 - prob)) if prob >= .5 else round(100 * (1 - prob) / prob)


def _decimal(odds: int) -> float:
    return 1 + (odds / 100 if odds > 0 else 100 / abs(odds))


def _init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hr_sniper_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluated_at TEXT NOT NULL,
            game_date TEXT NOT NULL,
            game_pk INTEGER NOT NULL,
            commence_time TEXT NOT NULL,
            player_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            team_abbr TEXT,
            opponent_abbr TEXT,
            batting_order INTEGER NOT NULL,
            pitcher_id INTEGER NOT NULL,
            pitcher_name TEXT NOT NULL,
            model_hr_probability REAL NOT NULL,
            fair_odds INTEGER NOT NULL,
            best_book TEXT NOT NULL,
            best_odds INTEGER NOT NULL,
            market_probability REAL NOT NULL,
            no_vig_market_probability REAL,
            edge_percentage_points REAL NOT NULL,
            expected_value_pct REAL NOT NULL,
            expected_pa REAL NOT NULL,
            confidence_score INTEGER NOT NULL,
            uncertainty_score INTEGER NOT NULL,
            classification TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            result TEXT DEFAULT NULL,
            actual_home_runs INTEGER DEFAULT NULL,
            graded_at TEXT DEFAULT NULL,
            UNIQUE(game_date, game_pk, player_id, best_book, best_odds)
        )
    """)
    conn.commit()


def _confirmed_lineups(date_str: str) -> dict[int, dict[str, list[dict]]]:
    data = sm.get_lineups_data(date_str) or {}
    out = {}
    for day in data.get("dates", []):
        for game in day.get("games", []):
            pk = game.get("gamePk")
            lineups = game.get("lineups") or {}
            home = lineups.get("homePlayers") or []
            away = lineups.get("awayPlayers") or []
            if not pk or not sm.has_confirmed_batting_order(home) or not sm.has_confirmed_batting_order(away):
                continue
            def rows(players):
                result = []
                for i, p in enumerate(players):
                    pos = (p.get("position") or p.get("primaryPosition") or {}).get("abbreviation")
                    if pos == "P" or not p.get("id"):
                        continue
                    raw = str(p.get("battingOrder") or "")
                    order = int(raw[0]) if raw and raw[0].isdigit() else i + 1
                    if 1 <= order <= 9:
                        result.append({
                            "id": int(p["id"]), "name": p.get("fullName") or p.get("name") or "",
                            "order": order, "bat_side": (p.get("batSide") or {}).get("code", ""),
                        })
                return sorted(result, key=lambda r: r["order"])[:9]
            out[int(pk)] = {"home": rows(home), "away": rows(away)}
    return out


def _odds_index(events: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Index validated .5 HR outcomes by normalized game, player, and book."""
    index = {}
    for event in events:
        home, away = _norm(event.get("home_team")), _norm(event.get("away_team"))
        game_key = "|".join(sorted((home, away)))
        for book in event.get("bookmakers") or []:
            book_key = book.get("key") or ""
            updated = book.get("last_update") or event.get("commence_time") or ""
            for market in book.get("markets") or []:
                if market.get("key") != "batter_home_runs":
                    continue
                outcomes = market.get("outcomes") or []
                for outcome in outcomes:
                    raw_name = outcome.get("name") or ""
                    side = _norm(raw_name)
                    player = outcome.get("description") or (raw_name if side not in ("over", "under", "yes", "no") else "")
                    if player and side not in ("over", "under", "yes", "no"):
                        side = "yes"
                    point = _f(outcome.get("point"), .5)
                    if side not in ("over", "under", "yes", "no") or abs(point - .5) > .01 or not player:
                        continue
                    key = (game_key, _norm(player), book_key)
                    rec = index.setdefault(key, {"book": book_key, "updated": updated})
                    rec["over" if side in ("over", "yes") else "under"] = int(outcome["price"])
    return index


def _market_for(game: dict, player_name: str, odds_index: dict) -> dict | None:
    game_key = "|".join(sorted((_norm(game["home_team_name"]), _norm(game["away_team_name"]))))
    offers = [v for (g, p, _), v in odds_index.items() if g == game_key and p == _norm(player_name) and "over" in v]
    fresh = []
    now = vortextime.vortex_now().astimezone(timezone.utc)
    for offer in offers:
        try:
            stamp = datetime.fromisoformat(str(offer.get("updated") or "").replace("Z", "+00:00"))
            if (now - stamp).total_seconds() / 60 > ODDS_MAX_AGE_MIN:
                continue
        except (TypeError, ValueError):
            continue
        fresh.append(offer)
    offers = fresh
    if not offers:
        return None
    best = max(offers, key=lambda x: _decimal(x["over"]))
    implied = [ub.american_to_implied(o["over"]) for o in offers]
    paired = []
    for offer in offers:
        if "under" in offer:
            po = ub.american_to_implied(offer["over"]); pu = ub.american_to_implied(offer["under"])
            paired.append(po / (po + pu))
    return {
        "odds": best["over"], "book": best["book"], "updated": best["updated"],
        "n_books": len(offers), "market_probability": ub.american_to_implied(best["over"]),
        "consensus_probability": sum(implied) / len(implied),
        "no_vig_probability": sum(paired) / len(paired) if paired else None,
    }


def _season_power(player_id: int, season: int) -> dict:
    data = sm._get(f"/people/{player_id}/stats", {
        "stats": "season", "group": "hitting", "season": season, "sportId": 1,
    }, cache_key=f"hrsniper_hit_{player_id}_{season}")
    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat") or {}
    pa = int(s.get("plateAppearances", 0) or 0)
    return {
        "pa": pa, "hr": int(s.get("homeRuns", 0) or 0), "ab": int(s.get("atBats", 0) or 0),
        "iso": _f(s.get("slg"), 0) - _f(s.get("avg"), 0), "ops": _f(s.get("ops")),
        "k_pct": int(s.get("strikeOuts", 0) or 0) / pa if pa else None,
    }


def _expected_pa(order: int, is_home: bool, team_ops: float | None, pitcher_fip: float | None) -> float:
    base = {1: 4.62, 2: 4.50, 3: 4.38, 4: 4.27, 5: 4.15, 6: 4.03, 7: 3.92, 8: 3.80, 9: 3.68}[order]
    base += -.08 if is_home else .04
    if team_ops:
        base += _clamp((team_ops - .720) * 1.2, -.10, .10)
    if pitcher_fip:
        base += _clamp((pitcher_fip - 4.10) * .025, -.06, .06)
    return round(_clamp(base, 3.35, 4.75), 2)


def _evaluate_one(game, player, is_home, market) -> dict:
    pitcher_id = game["away_pitcher_id"] if is_home else game["home_pitcher_id"]
    pitcher_name = game["away_pitcher"] if is_home else game["home_pitcher"]
    team_id = game["home_team_id"] if is_home else game["away_team_id"]
    team_abbr = game["home_abbr"] if is_home else game["away_abbr"]
    opp_abbr = game["away_abbr"] if is_home else game["home_abbr"]
    opp_team_id = game["away_team_id"] if is_home else game["home_team_id"]
    risks, positive, negative = [], [], []

    current = _season_power(player["id"], sm.SEASON)
    prior = _season_power(player["id"], sm.SEASON - 1)
    statcast = sm.get_statcast_by_id(player["id"]) or {}
    pitcher = sm.get_pitcher_metrics(pitcher_name, pitcher_id) if pitcher_id else {}
    pitcher_statcast = (game.get("_hr_pitcher_statcast") or {}).get(str(pitcher_id), {})
    batted_profile = (game.get("_hr_batted_profiles") or {}).get(str(player["id"]), {})
    pitcher_hand_splits = sm.get_pitcher_splits_by_hand(pitcher_id) if pitcher_id else {}
    batter_hand_splits = sm.get_batter_hand_splits(player["id"]) if pitcher_id else {}
    bullpen = (game.get("_hr_bullpens") or {}).get(opp_team_id, {})
    weather = game.get("_hr_weather") or {}
    team = sm.get_team_hitting_stats(team_id) or {}
    arsenal = sm.get_pitcher_arsenal(pitcher_id) if pitcher_id else []
    # Batter performance must come from the season-wide Savant pitch-arsenal
    # leaderboard.  The MLB vsPlayer/pitchArsenal endpoint is mostly frequency
    # data and was leaving every hitter with false low-coverage warnings.
    vs_pitch = sm.get_batter_arsenal_stats(player["id"])

    pa = current.get("pa", 0); hr = current.get("hr", 0)
    prior_pa = prior.get("pa", 0); prior_hr = prior.get("hr", 0)
    # Empirical-Bayes shrinkage: league prior + current season + discounted prior season.
    prior_weight = min(prior_pa, 450) * .35
    denom = 180 + pa + prior_weight
    hr_pa = (180 * LEAGUE_HR_PER_PA + hr + prior_hr * .35) / denom if denom else LEAGUE_HR_PER_PA
    if pa < 75:
        risks.append("LOW_BATTER_SAMPLE")
    # Savant's CSV parser can materialize a row of zeroes when the expected-
    # stats leaderboard has no real values for a low-sample hitter. Zero barrel
    # plus zero hard-hit plus no xSLG is missing data, not measured futility.
    _sc_barrel = _f(statcast.get("barrel_pct"))
    _sc_hard = _f(statcast.get("hard_hit_pct"))
    _sc_xslg = _f(statcast.get("xslg"))
    statcast_valid = bool(
        (_sc_barrel is not None and _sc_barrel > 0)
        or (_sc_hard is not None and _sc_hard > 0)
        or (_sc_xslg is not None and _sc_xslg > 0)
    )
    if not statcast_valid:
        risks.append("STATCAST_UNAVAILABLE")

    barrel = _sc_barrel if statcast_valid and _sc_barrel and _sc_barrel > 0 else None
    hard = _sc_hard if statcast_valid and _sc_hard and _sc_hard > 0 else None
    xslg = _sc_xslg if statcast_valid and _sc_xslg and _sc_xslg > 0 else None
    contact_multiplier = 1.0
    if barrel is not None:
        contact_multiplier *= math.exp(_clamp((barrel - 8.0) * .025, -.18, .22))
        (positive if barrel >= 11 else negative if barrel <= 5 else []).append(f"{barrel:.1f}% barrel rate")
    if hard is not None and barrel is None:  # correlated evidence: never double-count fully
        contact_multiplier *= math.exp(_clamp((hard - 39.0) * .012, -.12, .12))
    if xslg is not None:
        contact_multiplier *= math.exp(_clamp((xslg - .420) * .35, -.08, .10))

    profile_bbe = int(_f(batted_profile.get("bbe"), 0) or 0)
    pull_air = _f(batted_profile.get("pull_air_rate"))
    fly_ball = _f(batted_profile.get("fly_ball_rate"))
    sweet_spot = _f(statcast.get("sweet_spot_pct"))
    shape_multiplier = 1.0
    if profile_bbe >= 25 and pull_air is not None and fly_ball is not None:
        sample_weight = profile_bbe / (profile_bbe + 75)
        shape_signal = ((pull_air - .175) * .80 + (fly_ball - .25) * .25) * sample_weight
        if sweet_spot is not None and sweet_spot > 0:
            shape_signal += _clamp((sweet_spot - 33) * .002, -.025, .025)
        shape_multiplier = math.exp(_clamp(shape_signal, -.10, .12))
        if pull_air >= .22: positive.append(f"{pull_air*100:.1f}% pull-air rate ({profile_bbe} BBE)")
        elif pull_air <= .12: negative.append(f"{pull_air*100:.1f}% pull-air rate ({profile_bbe} BBE)")
    else:
        risks.append("BATTED_BALL_SHAPE_LOW_SAMPLE")

    pitcher_hand = (pitcher.get("hand") or "R").upper()
    bat_side = (player.get("bat_side") or sm.get_batter_hand(player["id"]) or "R").upper()
    effective_bat_side = ("L" if pitcher_hand == "R" else "R") if bat_side == "S" else bat_side
    hand_row = batter_hand_splits.get(pitcher_hand) or {}
    hand_pa = int(_f(hand_row.get("pa"), 0) or 0)
    hand_hr = int(_f(hand_row.get("hr"), 0) or 0)
    platoon_multiplier = 1.0
    if hand_pa >= 30:
        split_hr_pa = hand_hr / hand_pa
        split_weight = hand_pa / (hand_pa + 150)
        platoon_rate = split_weight * split_hr_pa + (1 - split_weight) * hr_pa
        platoon_multiplier = math.exp(_clamp((platoon_rate - hr_pa) / max(hr_pa, .015) * .18, -.14, .14))
        if platoon_multiplier >= 1.06:
            positive.append(f"{split_hr_pa*100:.1f}% HR/PA vs {pitcher_hand}HP ({hand_pa} PA)")
        elif platoon_multiplier <= .94:
            negative.append(f"{split_hr_pa*100:.1f}% HR/PA vs {pitcher_hand}HP ({hand_pa} PA)")
    else:
        risks.append("PLATOON_POWER_LOW_SAMPLE")

    hr9 = _f(pitcher.get("hr_per_9")); fip = _f(pitcher.get("fip")); pit_ip = _f(pitcher.get("innings_pitched"), 0)
    pitcher_multiplier = 1.0
    if hr9 is not None:
        shrink = pit_ip / (pit_ip + 60)
        regressed_hr9 = shrink * hr9 + (1 - shrink) * 1.15
        pitcher_multiplier *= math.exp(_clamp((regressed_hr9 - 1.15) * .22, -.16, .20))
        (positive if regressed_hr9 >= 1.35 else negative if regressed_hr9 <= .85 else []).append(f"starter {regressed_hr9:.2f} regressed HR/9")
    else:
        risks.append("PITCHER_HR_DATA_MISSING")

    pit_barrel = _f(pitcher_statcast.get("barrel_pct"))
    pit_hard = _f(pitcher_statcast.get("hard_hit_pct"))
    pitcher_contact_multiplier = 1.0
    if pit_barrel is not None and pit_barrel > 0:
        pitcher_contact_multiplier *= math.exp(_clamp((pit_barrel - 7.5) * .018, -.10, .14))
        if pit_barrel >= 10.0: positive.append(f"starter allows {pit_barrel:.1f}% barrels")
        elif pit_barrel <= 5.0: negative.append(f"starter allows only {pit_barrel:.1f}% barrels")
    elif pit_hard is not None and pit_hard > 0:
        pitcher_contact_multiplier *= math.exp(_clamp((pit_hard - 39.0) * .008, -.08, .10))
    else:
        risks.append("PITCHER_CONTACT_QUALITY_MISSING")

    pit_split = pitcher_hand_splits.get("vs_left" if effective_bat_side == "L" else "vs_right") or {}
    split_ip = _f(pit_split.get("ip"), 0) or 0
    split_fip = _f(pit_split.get("fip"))
    pitcher_platoon_multiplier = 1.0
    if split_fip is not None and split_ip >= 10:
        weight = split_ip / (split_ip + 40)
        regressed_fip = weight * split_fip + (1 - weight) * (_f(fip, 4.10) or 4.10)
        pitcher_platoon_multiplier = math.exp(_clamp((regressed_fip - 4.10) * .025, -.08, .08))

    pitch_rows = {r.get("pitch_type"): r for r in vs_pitch}
    weighted = coverage = 0.0
    for pitch in arsenal[:4]:
        row = pitch_rows.get(pitch.get("pitch_type")); usage = _f(pitch.get("pct"), 0)
        metric = _f((row or {}).get("woba"))
        sample = int(_f((row or {}).get("pa"), 0) or 0)
        if row and usage >= 10 and metric and sample >= 20:
            weighted += usage * metric; coverage += usage
    pitch_multiplier = 1.0
    pitch_mix = weighted / coverage if coverage >= 30 else None
    if pitch_mix is not None:
        pitch_multiplier = math.exp(_clamp((pitch_mix - .320) * .65, -.10, .12))
        (positive if pitch_mix >= .360 else negative if pitch_mix <= .280 else []).append(f"{pitch_mix:.3f} usage-weighted pitch-mix wOBA")
    else:
        risks.append("PITCH_MIX_LOW_COVERAGE")

    # Generic run environment remains audit-only. Prediction uses Savant's
    # three-year rolling HR park index for this hitter's effective bat side.
    generic_run_park = sm.PARK_FACTOR.get(game["home_team_name"], 1.0)
    park_table = game.get("_hr_park_factors") or {}
    park_index = _f((park_table.get(str(game.get("home_team_id"))) or {}).get(effective_bat_side))
    park_multiplier = 1.0
    if park_index is not None:
        # Shrink the observed park effect and cap it so venue cannot dominate.
        park_multiplier = _clamp(1 + (park_index / 100 - 1) * .65, .86, 1.16)
        if park_index >= 108: positive.append(f"{park_index:.0f} HR park index vs {effective_bat_side}HB")
        elif park_index <= 92: negative.append(f"{park_index:.0f} HR park index vs {effective_bat_side}HB")
    else:
        risks.append("DIRECTIONAL_HR_PARK_UNAVAILABLE")
    stadium = sm.STADIUM_DATA.get((game.get("home_abbr") or "").upper())
    if not stadium:
        risks.append("STADIUM_METADATA_MISSING")
    weather_multiplier = 1.0
    weather_error = weather.get("error")
    precip = _f(weather.get("precip_probability_pct"), 0) or 0
    precip_mm = _f(weather.get("precip_amount_mm"), 0) or 0
    if weather_error:
        risks.append("WEATHER_OR_ROOF_UNVERIFIED")
    elif weather.get("dome"):
        pass
    else:
        temp = _f(weather.get("temp_f"))
        pressure = _f(weather.get("pressure_hpa"))
        humidity = _f(weather.get("humidity_pct"))
        wind = _f(weather.get("speed_mph"), 0) or 0
        # One grouped air-density adjustment prevents temperature, pressure,
        # humidity and elevation from being counted as four separate bonuses.
        density_signal = 0.0
        if temp is not None: density_signal += (temp - 72) * .004
        if pressure is not None: density_signal += (1013 - pressure) * .0007
        if humidity is not None: density_signal += (humidity - 50) * .0003
        density_signal += (_f(weather.get("elevation_ft"), 0) or 0) / 5200 * .07
        wind_signal = 0.0
        if weather.get("hitter_friendly") is True: wind_signal = min(wind, 18) * .009
        elif weather.get("hitter_friendly") is False: wind_signal = -min(wind, 18) * .009
        weather_multiplier = math.exp(_clamp(density_signal + wind_signal, -.18, .22))
        if weather_multiplier >= 1.07: positive.append(f"{weather.get('temp_f')}F, {weather.get('effect')} weather")
        elif weather_multiplier <= .93: negative.append(f"{weather.get('temp_f')}F, {weather.get('effect')} weather")
        if precip >= 40: risks.append("PRECIPITATION_RISK")
        elif precip_mm >= .5: risks.append("PRECIPITATION_RISK")
        if weather.get("precip_probability_pct") is None:
            risks.append("PRECIP_PROBABILITY_UNAVAILABLE")
        if weather.get("stale_fallback"):
            risks.append("WEATHER_LAST_KNOWN_GOOD")

    bullpen_multiplier = 1.0
    bullpen_hr9 = _f(bullpen.get("hr9"))
    if bullpen.get("model_usable") and bullpen_hr9 is not None:
        bullpen_multiplier = math.exp(_clamp((bullpen_hr9 - 1.15) * .16, -.12, .16))
        if bullpen_hr9 >= 1.45: positive.append(f"bullpen {bullpen_hr9:.2f} HR/9")
        elif bullpen_hr9 <= .85: negative.append(f"bullpen {bullpen_hr9:.2f} HR/9")
        if int(bullpen.get("fatigued_count") or 0) >= 3:
            bullpen_multiplier *= 1.03
    else:
        risks.append("BULLPEN_HR_DATA_LOW_SAMPLE")

    expected_pa = _expected_pa(player["order"], is_home, _f(team.get("ops")), fip)
    team_runs = _f(team.get("runs_pg"))
    bullpen_era = _f(bullpen.get("era"))
    if team_runs is not None:
        expected_pa += _clamp((team_runs - 4.4) * .04, -.06, .06)
    if bullpen_era is not None:
        expected_pa += _clamp((bullpen_era - 4.10) * .02, -.04, .04)
    expected_pa = round(_clamp(expected_pa, 3.35, 4.75), 2)
    starter_share = _clamp((_f(pitcher.get("avg_ip_l3"), 5.2) or 5.2) / 9, .42, .72)
    starter_pa = expected_pa * starter_share
    bullpen_pa = expected_pa - starter_pa
    batter_pa_prob = _clamp(hr_pa * contact_multiplier * shape_multiplier * platoon_multiplier, .004, .115)
    starter_pa_prob = _clamp(
        batter_pa_prob * pitcher_multiplier * pitcher_contact_multiplier
        * pitcher_platoon_multiplier * pitch_multiplier * park_multiplier * weather_multiplier,
        .003, .14,
    )
    bullpen_pa_prob = _clamp(batter_pa_prob * bullpen_multiplier * park_multiplier * weather_multiplier, .003, .12)
    model_prob = 1 - ((1 - starter_pa_prob) ** starter_pa) * ((1 - bullpen_pa_prob) ** bullpen_pa)
    model_prob = _clamp(model_prob, .015, .48)

    uncertainty = 12
    uncertainty += 18 if pa < 75 else 9 if pa < 180 else 3
    uncertainty += 15 if not statcast_valid else 0
    uncertainty += 8 if hr9 is None else 0
    uncertainty += 7 if pitch_mix is None else 0
    uncertainty += 3 if park_index is None else 0
    uncertainty += 5 if "PITCHER_CONTACT_QUALITY_MISSING" in risks else 0
    uncertainty += 4 if "BATTED_BALL_SHAPE_LOW_SAMPLE" in risks else 0
    uncertainty += 4 if "BULLPEN_HR_DATA_LOW_SAMPLE" in risks else 0
    uncertainty += 5 if "WEATHER_OR_ROOF_UNVERIFIED" in risks else 0
    uncertainty += 3 if "WEATHER_LAST_KNOWN_GOOD" in risks else 0
    uncertainty += 5 if market["n_books"] < 2 else 0
    uncertainty = round(_clamp(uncertainty, 5, 65))
    confidence = 100 - uncertainty
    market_prob = market["market_probability"]
    no_vig = market.get("no_vig_probability")
    comparison = no_vig if no_vig is not None else market_prob
    edge_pp = (model_prob - comparison) * 100
    ev_pct = (model_prob * _decimal(market["odds"]) - 1) * 100
    risk_adjusted = ev_pct - uncertainty * .18

    critical_missing = {"STATCAST_UNAVAILABLE", "PITCHER_HR_DATA_MISSING", "STADIUM_METADATA_MISSING",
                        "WEATHER_OR_ROOF_UNVERIFIED"}
    if precip >= 60 or precip_mm >= 2.0:
        critical_missing.add("PRECIPITATION_RISK")
    has_structural_positive = bool(positive)
    eligible = bool(
        pitcher_id and pitcher_name and market["n_books"] >= MIN_BOOKS
        and not critical_missing.intersection(risks)
    )
    premium_data = bool(pitch_mix is not None and park_index is not None and profile_bbe >= 25 and
                        "PITCHER_CONTACT_QUALITY_MISSING" not in risks)
    if not eligible or not has_structural_positive or confidence < 50 or ev_pct <= 0: classification = "PASS"
    elif premium_data and risk_adjusted >= 18 and edge_pp >= 4 and confidence >= 70: classification = "SNIPER"
    elif premium_data and risk_adjusted >= 10 and edge_pp >= 3 and confidence >= 60: classification = "STRONG"
    elif risk_adjusted >= 5 and edge_pp >= 1.5 and confidence >= 65: classification = "LEAN"
    else: classification = "RISKY"

    return {
        "game_date": vortextime.vortex_board_day(), "game_pk": game["gamePk"], "commence_time": game["game_utc"],
        "player_id": player["id"], "player_name": player["name"], "team_abbr": team_abbr,
        "opponent_abbr": opp_abbr, "batting_order": player["order"], "pitcher_id": pitcher_id,
        "pitcher_name": pitcher_name, "model_hr_probability": round(model_prob, 5),
        "fair_odds": _american_fair(model_prob), "best_book": market["book"], "best_odds": market["odds"],
        "market_probability": round(market_prob, 5), "no_vig_market_probability": no_vig,
        "edge_percentage_points": round(edge_pp, 2), "expected_value_pct": round(ev_pct, 2),
        "risk_adjusted_edge": round(risk_adjusted, 2), "expected_pa": expected_pa,
        "confidence_score": confidence, "uncertainty_score": uncertainty,
        "classification": classification, "eligible": eligible,
        "positive_factors": positive, "negative_factors": negative, "risk_flags": risks,
        "inputs": {"season_pa": pa, "season_hr": hr, "prior_pa": prior_pa, "prior_hr": prior_hr,
                   "shrunk_hr_per_pa": round(hr_pa, 5), "barrel_pct": barrel, "hard_hit_pct": hard,
                   "xslg": xslg, "pitcher_hr9": hr9, "pitch_mix_woba": pitch_mix,
                   "pitch_mix_coverage": round(coverage, 1),
                   "generic_run_park_factor_context_only": generic_run_park,
                   "handed_hr_park_index": park_index,
                   "batter_side": bat_side, "pitcher_hand": pitcher_hand,
                   "platoon_hr_pa": round(hand_hr / hand_pa, 5) if hand_pa else None,
                   "platoon_pa": hand_pa, "pitcher_barrel_pct_allowed": pit_barrel,
                   "pitcher_hard_hit_pct_allowed": pit_hard, "pitcher_split_fip": split_fip,
                   "batted_ball_events": profile_bbe, "pull_air_rate": pull_air,
                   "fly_ball_rate": fly_ball, "sweet_spot_pct": sweet_spot,
                   "bullpen_hr9": bullpen_hr9, "bullpen_fatigued_count": bullpen.get("fatigued_count"),
                   "weather": weather,
                   "starter_pa": round(starter_pa, 2), "bullpen_pa": round(bullpen_pa, 2),
                   "n_books": market["n_books"], "odds_updated": market.get("updated"),
                   "model_version": "hr_sniper_v2_environment"},
    }


def _save(rows: list[dict]):
    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)
    evaluated = vortextime.vortex_now().astimezone(timezone.utc).isoformat()
    for row in rows:
        conn.execute("""
            INSERT OR IGNORE INTO hr_sniper_candidates
            (evaluated_at,game_date,game_pk,commence_time,player_id,player_name,team_abbr,opponent_abbr,
             batting_order,pitcher_id,pitcher_name,model_hr_probability,fair_odds,best_book,best_odds,
             market_probability,no_vig_market_probability,edge_percentage_points,expected_value_pct,
             expected_pa,confidence_score,uncertainty_score,classification,eligible,data_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (evaluated,row["game_date"],row["game_pk"],row["commence_time"],row["player_id"],row["player_name"],
              row["team_abbr"],row["opponent_abbr"],row["batting_order"],row["pitcher_id"],row["pitcher_name"],
              row["model_hr_probability"],row["fair_odds"],row["best_book"],row["best_odds"],
              row["market_probability"],row["no_vig_market_probability"],row["edge_percentage_points"],
              row["expected_value_pct"],row["expected_pa"],row["confidence_score"],row["uncertainty_score"],
              row["classification"],int(row["eligible"]),json.dumps(row, default=str)))
    conn.commit(); conn.close()


def evaluate_slate(date_str: str | None = None) -> dict:
    date_str = date_str or vortextime.vortex_board_day()
    schedule = sm.get_todays_schedule(game_date=date_str, fresh=True)
    lineups = _confirmed_lineups(date_str)
    odds_events = ub.fetch_props("baseball_mlb", "batter_home_runs")
    odds = _odds_index(odds_events)
    pitcher_statcast = nrfi._load_pitcher_statcast_leaderboard()
    hr_park_factors = sm.get_hr_park_factors_by_hand()
    batted_profiles = sm.get_batter_batted_ball_profiles()

    # Fetch each game environment and each bullpen once, before per-hitter
    # evaluation. This avoids 18 duplicate weather/roof/bullpen calls per game.
    active_games = [schedule[pk] for pk in lineups if pk in schedule]
    with ThreadPoolExecutor(max_workers=6) as context_pool:
        weather_futures = {
            context_pool.submit(sm.get_commercial_game_weather, g.get("home_abbr", ""),
                                g.get("game_utc", ""), g.get("gamePk")): g
            for g in active_games
        }
        team_ids = {tid for g in active_games for tid in (g.get("home_team_id"), g.get("away_team_id")) if tid}
        bullpen_futures = {context_pool.submit(sm.get_bullpen_stats, tid): tid for tid in team_ids}
        bullpens = {}
        for future, tid in bullpen_futures.items():
            try: bullpens[tid] = future.result()
            except Exception: bullpens[tid] = {}
        for future, game in weather_futures.items():
            try: game["_hr_weather"] = future.result()
            except Exception as exc: game["_hr_weather"] = {"error": str(exc)}
            game["_hr_bullpens"] = bullpens
            game["_hr_pitcher_statcast"] = pitcher_statcast
            game["_hr_park_factors"] = hr_park_factors
            game["_hr_batted_profiles"] = batted_profiles
    tasks = []
    now = vortextime.vortex_now().astimezone(timezone.utc)
    for pk, sides in lineups.items():
        game = schedule.get(pk)
        if not game or not game.get("home_pitcher_id") or not game.get("away_pitcher_id"):
            continue
        try:
            if datetime.fromisoformat(game["game_utc"].replace("Z", "+00:00")) <= now:
                continue
        except (TypeError, ValueError):
            continue
        for side, is_home in (("home", True), ("away", False)):
            for player in sides[side]:
                market = _market_for(game, player["name"], odds)
                if market:
                    tasks.append((game, player, is_home, market))

    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_evaluate_one, *task) for task in tasks]
        for future in as_completed(futures):
            try: rows.append(future.result())
            except Exception: continue
    tier_rank = {"SNIPER": 5, "STRONG": 4, "LEAN": 3, "RISKY": 2, "PASS": 1}
    rows.sort(key=lambda r: (tier_rank.get(r["classification"], 0), r["risk_adjusted_edge"]), reverse=True)
    _save(rows)
    return {"date": date_str, "confirmed_games": len(lineups), "market_candidates": len(tasks), "evaluated": rows}


def grade_date(date_str: str | None = None) -> dict:
    date_str = date_str or vortextime.vortex_day()
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; _init_db(conn)
    pending = conn.execute("SELECT * FROM hr_sniper_candidates WHERE game_date=? AND result IS NULL", (date_str,)).fetchall()
    if not pending:
        conn.close(); return {"pending": 0, "graded": 0}
    stats = {}; final_games = set()
    schedule = sm._get("/schedule", {"sportId": 1, "date": date_str, "hydrate": "linescore"}, cache_key=None) or {}
    final_pks = {g.get("gamePk") for d in schedule.get("dates", []) for g in d.get("games", []) if g.get("status", {}).get("abstractGameState") == "Final"}
    for pk in final_pks:
        final_games.add(int(pk))
        box = sm._get(f"/game/{pk}/boxscore", {}, cache_key=None) or {}
        for side in ("home", "away"):
            for pdata in (box.get("teams", {}).get(side, {}).get("players", {}) or {}).values():
                pid = pdata.get("person", {}).get("id")
                batting = pdata.get("stats", {}).get("batting", {}) or {}
                if pid:
                    pa = int(batting.get("plateAppearances", 0) or 0)
                    stats[(int(pk), int(pid))] = {
                        "home_runs": int(batting.get("homeRuns", 0) or 0), "played": pa > 0,
                    }
    graded_at = vortextime.vortex_now().astimezone(timezone.utc).isoformat(); graded = 0
    for row in pending:
        key = (row["game_pk"], row["player_id"])
        if key not in stats:
            if row["game_pk"] not in final_games: continue
            actual = None; result = "void"
        elif not stats[key]["played"]:
            actual = None; result = "void"
        else:
            actual = stats[key]["home_runs"]; result = "hit" if actual >= 1 else "miss"
        conn.execute("UPDATE hr_sniper_candidates SET result=?,actual_home_runs=?,graded_at=? WHERE id=?", (result, actual, graded_at, row["id"])); graded += 1
    conn.commit(); conn.close()
    return {"pending": len(pending), "graded": graded}
