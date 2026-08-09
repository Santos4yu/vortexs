"""Publish the Discord bot's decision-ready markets to the website's KV feed."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests


SITE_SPECIALS_KEY = "vortex:site_specials"


def _rows(query: str) -> list[dict]:
    db = Path(__file__).resolve().parent.parent / "vortex.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = [dict(row) for row in conn.execute(query).fetchall()]
        conn.close()
        return result
    except sqlite3.Error:
        return []


def _latest_slate_rows(table: str, columns: str, limit: str = "") -> list[dict]:
    """Use the bot's most recent slate, not the website host's calendar day."""
    return _rows(
        f"SELECT {columns} FROM {table} WHERE game_date=(SELECT MAX(game_date) FROM {table}) "
        f"ORDER BY id DESC {limit}"
    )


def _history_rows(table: str, columns: str, limit: int = 2000) -> list[dict]:
    """Return durable newest-first history instead of only the latest slate."""
    return _rows(
        f"SELECT {columns} FROM {table} ORDER BY game_date DESC, id DESC LIMIT {int(limit)}"
    )


def _prediction_key(row: dict) -> tuple:
    return (
        row.get("game_date"), str(row.get("player_name") or "").casefold(),
        row.get("market_key"), float(row.get("line") or 0), row.get("side"),
    )


def restore_prediction_history() -> int:
    """Restore remotely backed-up prop history after an ephemeral-host restart."""
    url = (os.getenv("KV_REST_API_URL") or "").rstrip("/")
    token = os.getenv("KV_REST_API_TOKEN") or ""
    if not url or not token:
        return 0
    try:
        response = requests.get(
            f"{url}/get/{SITE_SPECIALS_KEY}",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
        previous = json.loads(response.json().get("result") or "{}") if response.ok else {}
        remote = [r for r in ((previous.get("records") or {}).get("props") or [])
                  if str(r.get("sport") or "").upper() != "WNBA"]
    except (requests.RequestException, ValueError, AttributeError):
        return 0
    if not remote:
        return 0

    db = Path(__file__).resolve().parent.parent / "vortex.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    existing = {_prediction_key(dict(r)) for r in conn.execute(
        "SELECT game_date,player_name,market_key,line,side FROM predictions"
    )}
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for row in remote:
        if _prediction_key(row) in existing:
            continue
        required = (row.get("game_date"), row.get("player_name"),
                    row.get("stat_type"), row.get("market_key"), row.get("side"))
        if not all(required) or row.get("line") is None:
            continue
        conn.execute("""
            INSERT INTO predictions
              (logged_at,game_date,sport,player_name,stat_type,market_key,line,side,
               tier,vortex_score,matchup_score,matchup_label,result,actual_value,commence_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now, row["game_date"], row.get("sport") or "baseball_mlb",
              row["player_name"], row["stat_type"], row["market_key"], row["line"],
              row["side"], row.get("tier"), row.get("vortex_score"),
              row.get("matchup_score"), row.get("matchup_label"), row.get("result"),
              row.get("actual_value"), row.get("commence_time")))
        existing.add(_prediction_key(row))
        inserted += 1
    conn.commit()
    conn.close()
    return inserted


def publish_specials(moneylines: list[dict] | None = None, nrfis: list[dict] | None = None,
                     moneyline_research: list[dict] | None = None) -> bool:
    """Mirror active markets and today's settled records for the member app."""
    url = (os.getenv("KV_REST_API_URL") or "").rstrip("/")
    token = os.getenv("KV_REST_API_TOKEN") or ""
    if not url or not token:
        return False

    previous = {}
    try:
        old = requests.get(f"{url}/get/{SITE_SPECIALS_KEY}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        previous = json.loads(old.json().get("result") or "{}") if old.ok else {}
    except (requests.RequestException, ValueError, AttributeError):
        previous = {}
    local_props = _history_rows("predictions", "game_date, sport, player_name, stat_type, market_key, line, side, tier, vortex_score, matchup_score, matchup_label, result, actual_value, commence_time")
    old_props = [r for r in ((previous.get("records") or {}).get("props") or [])
                 if str(r.get("sport") or "").upper() != "WNBA"]
    merged_props = {_prediction_key(r): r for r in old_props}
    for row in reversed(local_props):
        merged_props[_prediction_key(row)] = row
    durable_props = list(merged_props.values())
    durable_props.sort(key=lambda r: (r.get("game_date") or "", r.get("commence_time") or ""), reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "moneylines": moneylines if moneylines is not None else previous.get("moneylines", []),
        "moneyline_research": moneyline_research if moneyline_research is not None else previous.get("moneyline_research", []),
        "nrfi": nrfis if nrfis is not None else previous.get("nrfi", []),
        "records": {
            "props": durable_props[:5000],
            "moneyline": _history_rows("moneyline_predictions", "game_date, rec_team, opponent, odds, model_pct, edge_pct, tier, result, actual_winner"),
            "nrfi": _history_rows("nrfi_predictions", "game_date, away_abbr, home_abbr, recommendation, score, confidence, result, actual_result, first_inning_away_runs, first_inning_home_runs"),
        },
    }
    try:
        response = requests.post(
            f"{url}/set/{SITE_SPECIALS_KEY}", data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
        return response.ok
    except requests.RequestException:
        return False
