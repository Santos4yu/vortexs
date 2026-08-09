"""WNBA orchestration: collect, evaluate, persist, query, and grade."""
from __future__ import annotations

import json, sqlite3
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, median

from . import data, odds
from .model import MODEL_VERSION, WNBAInput, evaluate_prop

DB = Path(__file__).resolve().parents[2] / "vortex.db"


def _conn():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row; return conn


def _projected_minutes(log: list[dict]) -> tuple[float, float]:
    minutes = [r["minutes"] for r in log if r["minutes"] > 0]
    if not minutes: return 0, 0
    season = mean(minutes)
    recent = minutes[:10]
    projection = .65 * mean(recent) + .35 * season
    return round(projection, 2), round(season, 2)


def _rates(log: list[dict], prop: str) -> tuple[float, float]:
    values = data.prop_values(log, prop); minutes = [r["minutes"] for r in log]
    pairs = [(v, m) for v, m in zip(values, minutes) if m >= 8]
    if not pairs: return 0, 0
    season = sum(v for v, _ in pairs) / sum(m for _, m in pairs)
    recent = pairs[:10]
    return season, sum(v for v, _ in recent) / sum(m for _, m in recent)


def _game_date(commence_time: str) -> str:
    return datetime.fromisoformat(commence_time.replace("Z", "+00:00")).astimezone(
        timezone(timedelta(hours=-7))).date().isoformat()


def _shot_factors(log: list[dict], prop_type: str) -> tuple[float, float]:
    if prop_type not in {"points", "threes", "pts_reb", "pts_ast", "pts_reb_ast"}:
        return 1.0, 1.0
    season = [g for g in log if g.get("minutes", 0) >= 8]
    recent = season[:10]
    if len(season) < 10 or len(recent) < 5: return 1.0, 1.0
    attempt_key = "tpa" if prop_type == "threes" else "fga"
    season_apm = sum(g.get(attempt_key, 0) for g in season) / sum(g["minutes"] for g in season)
    recent_apm = sum(g.get(attempt_key, 0) for g in recent) / sum(g["minutes"] for g in recent)
    volume = min(1.06, max(.94, recent_apm / season_apm)) if season_apm else 1.0
    if prop_type == "threes":
        season_eff = sum(g.get("threes", 0) for g in season) / max(1, sum(g.get("tpa", 0) for g in season))
        recent_eff = sum(g.get("threes", 0) for g in recent) / max(1, sum(g.get("tpa", 0) for g in recent))
    else:
        season_eff = sum(g.get("points", 0) for g in season) / max(1, sum(g.get("fga", 0) for g in season))
        recent_eff = sum(g.get("points", 0) for g in recent) / max(1, sum(g.get("fga", 0) for g in recent))
    # Regress only half of an efficiency deviation; volume is more persistent.
    regression = min(1.05, max(.90, 1 - .5 * ((recent_eff / season_eff) - 1))) if season_eff else 1.0
    return volume, regression


def _split_factor(log: list[dict], prop_type: str, predicate, target: int,
                  lower: float, upper: float) -> tuple[float, int]:
    """Return a per-minute contextual split, shrunk hard for small samples."""
    values = data.prop_values(log, prop_type)
    usable = [(value, row) for value, row in zip(values, log)
              if float(row.get("minutes") or 0) >= 8]
    sample = [(value, row) for value, row in usable if predicate(row)]
    if not usable or not sample:
        return 1.0, 0
    base_minutes = sum(float(row["minutes"]) for _, row in usable)
    split_minutes = sum(float(row["minutes"]) for _, row in sample)
    if base_minutes <= 0 or split_minutes <= 0:
        return 1.0, 0
    base_rate = sum(value for value, _ in usable) / base_minutes
    split_rate = sum(value for value, _ in sample) / split_minutes
    if base_rate <= 0:
        return 1.0, len(sample)
    raw = split_rate / base_rate
    weight = min(1.0, len(sample) / max(1, target))
    shrunk = 1.0 + (raw - 1.0) * weight
    return round(min(upper, max(lower, shrunk)), 4), len(sample)


def scan(force_odds: bool = False) -> dict:
    events, usage = odds.fetch(force_odds); markets, _ = odds.parse(events)
    if not markets:
        return {"evaluated": 0, "published": 0, "active": len(board(False)),
                "unresolved": 0, **usage}
    roster, games, report = data.roster_index(), data.opponent_map(data.schedule()), data.injuries()
    profiles = {team["id"]: data.team_profile(str(team["id"])) for team in data.teams()}
    paces = [p["pace"] for p in profiles.values() if p.get("pace")]
    league_pace = mean(paces) if paces else None
    posted_totals = [float(m["total"]) for m in markets if m.get("total")]
    slate_total = mean(posted_totals) if posted_totals else None
    points_allowed = [p["points_allowed"] for p in profiles.values() if p.get("points_allowed")]
    league_points_allowed = mean(points_allowed) if points_allowed else None
    unique_games = {game["event_id"]: game for game in games.values()}
    lineup_cache = {event_id: data.lineup_status(event_id) for event_id in unique_games}
    conn, evaluated, published, unresolved = _conn(), 0, 0, 0
    qualified = []
    timestamp = datetime.now(timezone.utc).isoformat()
    for market in markets:
        player = data.find_player(market["player_name"], roster)
        if not player or player["team"] not in games:
            unresolved += 1; continue
        matchup, log = games[player["team"]], data.game_log(player["id"])
        if len(log) < 5: unresolved += 1; continue
        projected_minutes, season_minutes = _projected_minutes(log)
        season_rpm, recent_rpm = _rates(log, market["prop_type"])
        usage_factor, efficiency_regression = _shot_factors(log, market["prop_type"])
        values = data.prop_values(log, market["prop_type"])
        venue_factor, venue_sample = _split_factor(
            log, market["prop_type"],
            lambda row: row.get("is_home") is matchup.get("is_home"),
            target=8, lower=.96, upper=1.04,
        )
        h2h_factor, h2h_sample = _split_factor(
            log, market["prop_type"],
            lambda row: str(row.get("opponent") or "").upper() == str(matchup["opponent"]).upper(),
            target=5, lower=.98, upper=1.02,
        )
        over_odds, under_odds, book = odds.best_prices(market)
        environment_factor = (min(1.04, max(.96, float(market["total"]) / slate_total))
                              if slate_total and market.get("total") else 1.0)
        opp_profile = profiles.get(matchup["opponent_id"], {})
        pace_factor = (opp_profile.get("pace") / league_pace) if league_pace and opp_profile.get("pace") else 1.0
        opponent_factor = 1.0
        if market["prop_type"] in {"points", "threes", "pts_reb", "pts_ast", "pts_reb_ast"} and league_points_allowed and opp_profile.get("points_allowed"):
            opponent_factor = min(1.06, max(.94, opp_profile["points_allowed"] / league_points_allowed))
        status = data.player_status(player["team_id"], player["name"], report)
        lineup = lineup_cache.get(matchup["event_id"], {})
        lineup_row = lineup.get(player["id"])
        back_to_back = data.is_back_to_back(player["team_id"], matchup["commence_time"])
        teammate_absences = [r["name"] for r in report.get(player["team_id"], [])
                             if r["status"] in {"out", "doubtful"} and r["name"] != player["name"]]
        for side in ("over", "under"):
            bettable = {k: v for k, v in market[side].items() if k != "pinnacle"}
            selected_book = max(bettable, key=bettable.get, default=book)
            selected_price = bettable.get(selected_book)
            inp = WNBAInput(
                player_id=player["id"], player_name=player["name"], team=player["team"],
                opponent=matchup["opponent"], game_date=_game_date(matchup["commence_time"]),
                commence_time=matchup["commence_time"], market_key=market["market_key"],
                prop_type=market["prop_type"], side=side, line=market["line"],
                over_odds=over_odds, under_odds=under_odds, best_book=selected_book,
                projected_minutes=projected_minutes, season_minutes=season_minutes,
                recent_minutes=[r["minutes"] for r in log[:15]], recent_values=values[:15],
                season_rate_per_minute=season_rpm, recent_rate_per_minute=recent_rpm,
                is_home=matchup.get("is_home"), venue_factor=venue_factor,
                venue_sample=venue_sample, h2h_factor=h2h_factor, h2h_sample=h2h_sample,
                role="starter" if (lineup_row or {}).get("starter") else ("projected starter" if projected_minutes >= 25 else "rotation"),
                availability=("out" if lineup_row and not lineup_row.get("active") else status),
                role_confirmed=lineup_row is not None, pace_factor=pace_factor,
                opponent_factor=opponent_factor,
                rest_factor=.98 if back_to_back else 1.0,
                game_environment_factor=environment_factor,
                usage_factor=usage_factor, efficiency_regression=efficiency_regression,
                spread=market.get("spread"), game_total=market.get("total"),
                books_count=len(set(market["over"]) | set(market["under"])),
                injury_data=bool(report), opponent_data=bool(opp_profile),
                warnings=((["back-to-back schedule"] if back_to_back else []) +
                          (["teammate absences require role confirmation: " + ", ".join(teammate_absences[:3])]
                           if teammate_absences else [])),
            )
            result = evaluate_prop(inp); evaluated += 1
            # Preserve the original recommendation while continuously recording
            # the latest available line/price for closing-line-value analysis.
            conn.execute("""UPDATE wnba_predictions SET closing_line=?,closing_odds=?
                WHERE game_date=? AND player_id=? AND market_key=? AND side=? AND result IS NULL""",
                (inp.line, selected_price, inp.game_date, inp.player_id, inp.market_key, inp.side))
            conn.execute("""
                INSERT INTO wnba_evaluations
                (evaluated_at,model_version,game_date,commence_time,player_id,player_name,team,opponent,
                 market_key,prop_type,side,line,over_odds,under_odds,best_book,projected_minutes,
                 projected_mean,projected_floor,projected_ceiling,standard_deviation,selected_probability,
                 market_probability,edge_pp,fair_odds,data_quality,variance_score,variance_label,tier,
                 publish,watchlist,reasons_json,risks_json,rejection_json,inputs_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(game_date,player_id,market_key,line,side,model_version) DO UPDATE SET
                 evaluated_at=excluded.evaluated_at, over_odds=excluded.over_odds,under_odds=excluded.under_odds,
                 best_book=excluded.best_book,projected_minutes=excluded.projected_minutes,
                 projected_mean=excluded.projected_mean,selected_probability=excluded.selected_probability,
                 market_probability=excluded.market_probability,edge_pp=excluded.edge_pp,data_quality=excluded.data_quality,
                 variance_score=excluded.variance_score,variance_label=excluded.variance_label,tier=excluded.tier,
                 publish=excluded.publish,watchlist=excluded.watchlist,reasons_json=excluded.reasons_json,
                 risks_json=excluded.risks_json,rejection_json=excluded.rejection_json,inputs_json=excluded.inputs_json
            """, (timestamp, MODEL_VERSION, inp.game_date, inp.commence_time, inp.player_id, inp.player_name,
                  inp.team, inp.opponent, inp.market_key, inp.prop_type, inp.side, inp.line, inp.over_odds,
                  inp.under_odds, inp.best_book, inp.projected_minutes, result.projected_mean,
                  result.projected_floor, result.projected_ceiling, result.standard_deviation,
                  result.selected_probability, result.market_probability, result.edge_pp, result.fair_odds,
                  result.data_quality, result.variance_score, result.variance_label, result.tier,
                  int(result.publish), int(result.watchlist), json.dumps(result.reasons), json.dumps(result.risks),
                  json.dumps(result.hard_rejections),
                  json.dumps({**asdict(inp), "model_diagnostics": result.diagnostics})))
            if result.publish:
                repeated = conn.execute("""SELECT COUNT(DISTINCT game_date) FROM wnba_predictions
                    WHERE player_id=? AND market_key=? AND side=? AND game_date < ?
                    AND game_date >= date(?, '-3 days')""",
                    (inp.player_id, inp.market_key, inp.side, inp.game_date, inp.game_date)).fetchone()[0]
                repeat_ok = (repeated == 0 or
                             (repeated == 1 and result.selected_probability >= .62 and (result.edge_pp or 0) >= 6) or
                             (repeated >= 2 and result.selected_probability >= .65 and (result.edge_pp or 0) >= 8))
                if repeat_ok:
                    evaluation_id = conn.execute("SELECT id FROM wnba_evaluations WHERE game_date=? AND player_id=? AND market_key=? AND line=? AND side=? AND model_version=?",
                        (inp.game_date, inp.player_id, inp.market_key, inp.line, inp.side, MODEL_VERSION)).fetchone()[0]
                    qualified.append((result.diagnostics["board_score"],
                                      evaluation_id, inp, result, selected_price))
                else:
                    conn.execute("UPDATE wnba_evaluations SET tier='WATCHLIST',publish=0,watchlist=1 WHERE game_date=? AND player_id=? AND market_key=? AND line=? AND side=? AND model_version=?",
                        (inp.game_date, inp.player_id, inp.market_key, inp.line, inp.side, MODEL_VERSION))

    # Official selections are exposure-controlled independently from evaluation:
    # one prop per player and four per game. Existing logged selections are immutable.
    selected_players, game_counts = set(), {}
    for _, evaluation_id, inp, result, selected_price in sorted(qualified, reverse=True, key=lambda item: item[0]):
        existing = conn.execute("SELECT 1 FROM wnba_predictions WHERE game_date=? AND player_id=?",
                                (inp.game_date, inp.player_id)).fetchone()
        player_key = (inp.game_date, inp.player_id)
        game_key = (inp.game_date, *sorted((inp.team, inp.opponent)))
        if player_key in selected_players or existing or game_counts.get(game_key, 0) >= 4:
            continue
        conn.execute("""INSERT OR IGNORE INTO wnba_predictions
            (logged_at,model_version,evaluation_id,game_date,commence_time,player_id,player_name,team,
             opponent,market_key,prop_type,side,line,selected_probability,market_probability,edge_pp,
             fair_odds,best_book,best_odds,over_odds,under_odds,data_quality,variance_label,tier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (timestamp,MODEL_VERSION,evaluation_id,inp.game_date,inp.commence_time,inp.player_id,
             inp.player_name,inp.team,inp.opponent,inp.market_key,inp.prop_type,inp.side,inp.line,
             result.selected_probability,result.market_probability,result.edge_pp,result.fair_odds,
             inp.best_book,selected_price,inp.over_odds,inp.under_odds,
             result.data_quality,result.variance_label,result.tier))
        selected_players.add(player_key); game_counts[game_key] = game_counts.get(game_key, 0) + 1
        published += 1
    conn.commit()
    active = conn.execute("SELECT COUNT(*) FROM wnba_predictions WHERE commence_time > ?",
                          (datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),)).fetchone()[0]
    conn.close()
    return {"evaluated": evaluated, "published": published, "active": active,
            "unresolved": unresolved, **usage}


def board(include_watchlist: bool = True) -> list[dict]:
    conn = _conn(); now = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    rows = [dict(r) for r in conn.execute("""SELECT p.*,e.projected_mean,e.projected_floor,
        e.projected_ceiling,e.reasons_json,e.risks_json,e.inputs_json
        FROM wnba_predictions p LEFT JOIN wnba_evaluations e ON e.id=p.evaluation_id
        WHERE p.commence_time > ?""", (now,))]
    if include_watchlist:
        official_players = {(r["game_date"], r["player_id"]) for r in rows}
        watches = [dict(r) for r in conn.execute("""SELECT * FROM wnba_evaluations
            WHERE tier='WATCHLIST' AND commence_time > ?
            ORDER BY selected_probability DESC,edge_pp DESC LIMIT 12""", (now,))]
        rows.extend(r for r in watches if (r["game_date"], r["player_id"]) not in official_players)
    conn.close()
    for row in rows:
        try:
            diagnostics = json.loads(row.get("inputs_json") or "{}").get("model_diagnostics", {})
        except (TypeError, ValueError):
            diagnostics = {}
        clearance = diagnostics.get("clearance") or {}
        row["board_score"] = float(diagnostics.get("board_score") or
                                   (float(row.get("selected_probability") or 0) * 100))
        row["clearance_label"] = clearance.get("label")
        row["comfortable_rate"] = clearance.get("comfortable_rate")
    rows.sort(key=lambda row: (row["board_score"], row.get("edge_pp") or -99), reverse=True)
    return rows


def record(game_date: str) -> list[dict]:
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM wnba_predictions WHERE game_date=? ORDER BY selected_probability DESC", (game_date,))]
    conn.close(); return rows


def player_research(name: str) -> list[dict]:
    conn = _conn()
    rows = [dict(r) for r in conn.execute("""SELECT * FROM wnba_evaluations
        WHERE lower(player_name) LIKE ? ORDER BY evaluated_at DESC,selected_probability DESC LIMIT 20""",
        (f"%{name.lower()}%",))]
    conn.close(); return rows


def calibration() -> dict:
    conn = _conn()
    rows = conn.execute("""SELECT tier,prop_type,side,selected_probability,result
        FROM wnba_predictions WHERE result IN ('hit','miss')""").fetchall(); conn.close()
    def summarize(items):
        n = len(items); hits = sum(r["result"] == "hit" for r in items)
        return {"n": n, "hits": hits, "rate": round(hits / n * 100, 1) if n else None,
                "expected": round(mean(r["selected_probability"] for r in items) * 100, 1) if n else None}
    return {"overall": summarize(rows),
            "tiers": {key: summarize([r for r in rows if r["tier"] == key]) for key in ("STRONG", "LEAN")},
            "props": {key: summarize([r for r in rows if r["prop_type"] == key]) for key in sorted({r["prop_type"] for r in rows})}}


def grade(game_date: str) -> dict:
    conn = _conn(); pending = conn.execute(
        "SELECT * FROM wnba_predictions WHERE game_date=? AND result IS NULL", (game_date,)).fetchall()
    roster = data.roster_index(); graded = unresolved = 0
    for row in pending:
        player = data.find_player(row["player_name"], roster)
        if not player: unresolved += 1; continue
        log = data.game_log(player["id"], fresh=True)
        def same_day(game):
            raw = str(game.get("date", ""))
            if raw[:10] == game_date: return True
            try: return _game_date(raw) == game_date
            except (ValueError, TypeError): return False
        game = next((g for g in log if same_day(g)), None)
        if not game:
            event_id = data.team_event_on_date(player["team_id"], game_date)
            if event_id and data.game_state(event_id) == "post":
                conn.execute("UPDATE wnba_predictions SET result='void',graded_at=? WHERE id=?",
                             (datetime.now(timezone.utc).isoformat(), row["id"])); graded += 1
            else: unresolved += 1
            continue
        if data.game_state(game["event_id"]) != "post":
            unresolved += 1; continue
        values = data.prop_values([game], row["prop_type"])
        if not values: unresolved += 1; continue
        actual = values[0]; line = float(row["line"]); side = row["side"]
        result = "push" if actual == line else "hit" if ((actual > line) == (side == "over")) else "miss"
        conn.execute("UPDATE wnba_predictions SET result=?,actual_value=?,actual_minutes=?,graded_at=? WHERE id=?",
                     (result, actual, game.get("minutes"), datetime.now(timezone.utc).isoformat(), row["id"]))
        graded += 1
    conn.commit(); conn.close()
    return {"pending": len(pending), "graded": graded, "unresolved": unresolved}
