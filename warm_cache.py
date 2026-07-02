"""
warm_cache.py  —  Run locally each day before starting the Wispbyte bot.

Since Wispbyte's IP is blocked by MLB's Cloudflare, we pre-fetch everything
the board needs here (where the API works), save it to cache, then upload
backend/cache/mlb_stats/ to Wispbyte.

Usage:
    python warm_cache.py
    # Then upload backend/cache/mlb_stats/ to Wispbyte via the Files tab.
"""

import sys
import sqlite3
import logging
from datetime import date
from pathlib import Path

# Add backend to path so we can import stats_mlb
sys.path.insert(0, str(Path(__file__).parent / "backend"))
import stats_mlb

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("warm_cache")

DB_PATH = Path(__file__).parent / "vortex.db"

def get_board_players() -> list[tuple[str, str]]:
    """Return (player_name, stat_type) for every MLB row on the active board."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT player_name, stat_type FROM active_board WHERE sport = 'MLB'"
    ).fetchall()
    conn.close()
    return rows


def warm():
    today = date.today().strftime("%Y-%m-%d")
    log.info("=== Warming MLB cache for %s ===", today)

    # ── 1. Schedule + probable pitchers ───────────────────────────────────────
    log.info("Fetching schedule...")
    schedule = stats_mlb.get_todays_schedule(today)
    log.info("  %d games found", len(schedule))

    # ── 2. Game times ─────────────────────────────────────────────────────────
    log.info("Fetching game times...")
    stats_mlb.get_todays_game_times()

    # ── 3. Umpires ────────────────────────────────────────────────────────────
    log.info("Fetching umpires...")
    stats_mlb.get_game_umpires()

    # ── 4. Team stats for every team playing today ────────────────────────────
    team_ids = set()
    pitcher_ids: list[tuple[str, int]] = []   # (name, id)
    for g in schedule.values():
        for side in ("home", "away"):
            tid = g.get(f"{side}_team_id")
            if tid:
                team_ids.add(tid)
        for side in ("home", "away"):
            pname = g.get(f"{side}_pitcher")
            pid   = g.get(f"{side}_pitcher_id")
            if pname and pid:
                pitcher_ids.append((pname, pid))

    log.info("Pre-fetching team hitting stats for %d teams...", len(team_ids))
    for tid in team_ids:
        stats_mlb.get_team_hitting_stats(tid)

    log.info("Pre-fetching team K rates for %d teams...", len(team_ids))
    for tid in team_ids:
        for hand in ("R", "L"):
            stats_mlb.get_team_k_rate_vs_hand(tid, hand)

    log.info("Pre-fetching bullpen stats for %d teams...", len(team_ids))
    for tid in team_ids:
        stats_mlb.get_bullpen_stats(tid)

    # ── 5. Pitcher cards for today's starters ─────────────────────────────────
    log.info("Pre-fetching pitcher metrics for %d probable starters...", len(pitcher_ids))
    for pname, pid in pitcher_ids:
        stats_mlb.get_pitcher_metrics(pname)
        stats_mlb.get_pitcher_arsenal(pid)

    # ── 6. All-teams K rate (used for matchup context) ────────────────────────
    log.info("Fetching all-teams K rate table...")
    stats_mlb.get_all_teams_k_rate()

    # ── 7. Board players ──────────────────────────────────────────────────────
    board_players = get_board_players()
    log.info("Pre-fetching data for %d board players...", len(board_players))

    for player_name, stat_type in board_players:
        log.info("  → %s (%s)", player_name, stat_type)

        pid = stats_mlb.get_player_id(player_name)
        if not pid:
            log.warning("    Could not resolve player ID for %s", player_name)
            continue

        # Current team
        team_id = stats_mlb.get_player_current_team(pid)

        # Historical splits — line doesn't matter for warming, use a typical value
        typical_lines = {
            "Hits": 1.5,
            "Total Bases": 1.5,
            "Home Runs": 0.5,
            "RBIs": 0.5,
            "Runs Scored": 0.5,
            "Strikeouts": 4.5,
            "Walks": 0.5,
            "Hits+Runs+RBIs": 2.5,
        }
        line = typical_lines.get(stat_type, 1.5)
        stats_mlb.get_historical_splits(pid, line, stat_type.lower().replace("+", "_").replace(" ", "_"))

        # Bat hand (for matchup context)
        stats_mlb.get_player_bat_side(pid)

        # Batter hand splits
        for hand in ("R", "L"):
            stats_mlb.get_batter_hand_splits(pid, hand)

        # Statcast metrics
        stats_mlb.get_statcast_by_id(pid)

        # BvP vs every pitcher playing today
        for pname, ppid in pitcher_ids:
            stats_mlb.get_bvp_history(pid, ppid)

        # Team defense OAA for current team
        if team_id:
            stats_mlb.get_team_defense_oaa(team_id)

    log.info("")
    log.info("=== Cache warm complete ===")
    log.info("Now upload backend/cache/mlb_stats/ to Wispbyte via the Files tab.")
    log.info("Cache TTL is 14h (volatile) / 48h (stats) — good for the full day.")


if __name__ == "__main__":
    warm()
