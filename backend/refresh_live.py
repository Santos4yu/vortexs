"""
VORTEX — Free Live Refresher
============================
Re-checks lineup status on the EXISTING board WITHOUT touching the Odds API.

Why this exists
---------------
The full board build (update_board.py) fetches odds (paid credits) AND stats in
one pass, so lineup/scratch info only refreshes when you rebuild the board —
expensive on a free Odds-API tier. This script reads the props already saved in
the database and updates them using only the FREE MLB Stats API:

  • Scratch removal   — lineup posted but the player isn't in it  → drop the prop
  • Confirmation flip — ⏳ unconfirmed → ✅ confirmed once the lineup is official
  • Demotion cap      — confirmed bottom-of-order (8–9) hitters get knocked a tier
                        on counting-stat Overs (fewer PAs than the prop assumed)

Run it as often as you like near game time (e.g. every 20–30 min). It costs
ZERO odds credits — it never calls the Odds API.

Usage:  python refresh_live.py
"""

import json
import sqlite3
from pathlib import Path

import stats_mlb

DB_PATH = Path(__file__).parent.parent / "vortex.db"

# Only upcoming games (don't touch props for games already started/final)
_LIVE_FILTER = (
    "(commence_time IS NOT NULL AND commence_time != '' "
    "AND commence_time > strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
)

# Counting-stat Overs that depend on plate-appearance volume → demote if batting low
_DEMOTE_KW = ("hits", "total base", "hits+run", "rbi", "run", "fantasy")
_DOWN_TIER = {"ELITE": "STRONG", "STRONG": "GOOD", "GOOD": "LEAN"}


def _is_pitcher_prop(stat_type: str) -> bool:
    """Pitcher props don't depend on a posted batting order — never gated."""
    s = (stat_type or "").lower()
    return ("strikeout" in s or "pitcher" in s or "earned run" in s
            or "outs" in s or "hits allowed" in s)


def refresh() -> dict:
    """
    Returns a summary dict: {checked, scratched, confirmed, demoted, errors}.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        f"SELECT * FROM props_board WHERE sport='MLB' AND {_LIVE_FILTER}"
    ).fetchall()

    # Cache lineup lookups per team so we hit the API once per team, not per prop.
    _team_lineup_cache: dict[int, list] = {}
    _pid_cache: dict[str, int] = {}
    _team_cache: dict[int, int] = {}

    summary = {"checked": 0, "scratched": 0, "confirmed": 0, "demoted": 0, "errors": 0}

    for r in rows:
        stat_type = r["stat_type"] or ""
        if _is_pitcher_prop(stat_type):
            continue  # pitcher props are not lineup-gated

        summary["checked"] += 1
        player = r["player_name"]

        try:
            sj = json.loads(r["stats_json"]) if r["stats_json"] else {}
        except (json.JSONDecodeError, TypeError):
            sj = {}

        try:
            # Resolve batter → team (cached)
            if player not in _pid_cache:
                _pid_cache[player] = stats_mlb.get_player_id(player)
            batter_id = _pid_cache[player]
            if not batter_id:
                continue

            if batter_id not in _team_cache:
                _team_cache[batter_id] = stats_mlb.get_player_current_team(batter_id)
            team_id = _team_cache[batter_id]
            if not team_id:
                continue

            # Lineup IDs for this team (cached). Empty = not posted yet.
            if team_id not in _team_lineup_cache:
                _team_lineup_cache[team_id] = stats_mlb.get_game_lineup_ids(team_id) or []
            lineup_ids = _team_lineup_cache[team_id]

            if not lineup_ids:
                # Lineup still unconfirmed — leave the prop as-is (stays ⏳).
                continue

            # Lineup IS posted ─────────────────────────────────────────────────
            if batter_id not in lineup_ids:
                # Scratched — remove from the board entirely.
                cur.execute("DELETE FROM props_board WHERE id = ?", (r["id"],))
                summary["scratched"] += 1
                print(f"  [scratch] {player} not in posted lineup → removed")
                continue

            # In the lineup — confirm + get position.
            try:
                lineup_pos = stats_mlb.get_lineup_position(batter_id)
            except Exception:
                lineup_pos = sj.get("lineup_pos")

            was_confirmed = sj.get("lineup_confirmed") is True
            sj["lineup_confirmed"] = True
            if isinstance(lineup_pos, int):
                sj["lineup_pos"] = lineup_pos
            if not was_confirmed:
                summary["confirmed"] += 1

            # Demotion cap: confirmed batting 8–9 on a volume-dependent Over.
            tier = r["tier"]
            side = sj.get("side", "over")
            if (isinstance(lineup_pos, int) and lineup_pos >= 8 and side == "over"
                    and any(kw in stat_type.lower() for kw in _DEMOTE_KW)
                    and tier in _DOWN_TIER):
                new_tier = _DOWN_TIER[tier]
                sj["tier"] = new_tier
                cur.execute(
                    "UPDATE props_board SET tier = ?, stats_json = ? WHERE id = ?",
                    (new_tier, json.dumps(sj, default=str), r["id"]),
                )
                summary["demoted"] += 1
                print(f"  [demote] {player} confirmed batting {lineup_pos} → {tier}→{new_tier}")
            else:
                cur.execute(
                    "UPDATE props_board SET stats_json = ? WHERE id = ?",
                    (json.dumps(sj, default=str), r["id"]),
                )

        except Exception as e:
            summary["errors"] += 1
            print(f"  [error] {player}: {e}")

    conn.commit()
    conn.close()
    return summary


if __name__ == "__main__":
    print("VORTEX live refresher — updating lineup status (no Odds API calls)...")
    s = refresh()
    print(
        f"\nDone. Checked {s['checked']} batter props · "
        f"{s['confirmed']} newly confirmed · {s['scratched']} scratched (removed) · "
        f"{s['demoted']} demoted · {s['errors']} errors."
    )
