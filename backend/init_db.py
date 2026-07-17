"""
Initialize / migrate the VORTEX database schema.
Run once to add new tables — safe to re-run (uses IF NOT EXISTS).
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "vortex.db"

def init():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # ── predictions: every prop VORTEX surfaces, logged before games ──────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at     TEXT    NOT NULL,          -- ISO timestamp when engine ran
            game_date     TEXT    NOT NULL,          -- YYYY-MM-DD
            sport         TEXT    NOT NULL,
            player_name   TEXT    NOT NULL,
            stat_type     TEXT    NOT NULL,
            market_key    TEXT    NOT NULL,
            line          REAL    NOT NULL,
            side          TEXT    NOT NULL,          -- 'over' | 'under'
            tier          TEXT,
            signal_type   TEXT,
            ev_percentage REAL,
            vortex_score  INTEGER,
            best_book     TEXT,
            best_odds     INTEGER,
            n_books       INTEGER,
            l5_rate       REAL,
            l10_rate      REAL,
            l20_rate      REAL,
            season_avg    REAL,
            pitcher_name  TEXT,
            pitcher_era   REAL,
            park_factor   REAL,
            -- new scoring sub-components (logged for weight learning)
            proj_edge      REAL    DEFAULT NULL,     -- L10 avg minus line (+ = edge for our side)
            damage_score   INTEGER DEFAULT NULL,     -- Barrel/HH/xSLG/xwOBA composite (0-6)
            stability_tier TEXT    DEFAULT NULL,     -- 'HIGH'|'MEDIUM'|'LOW'|'VOLATILE'
            lineup_spot    INTEGER DEFAULT NULL,     -- batting order position (1-9)
            commence_time  TEXT    DEFAULT NULL,     -- ISO UTC game start time
            -- result fields (filled in by grader)
            result        TEXT    DEFAULT NULL,      -- 'hit' | 'miss' | 'push'
            actual_value  REAL    DEFAULT NULL,      -- actual stat value
            graded_at     TEXT    DEFAULT NULL
        )
    """)

    # ── signal_accuracy: rolling accuracy per signal type / tier / stat ───────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_accuracy (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at    TEXT    NOT NULL,
            sport         TEXT    NOT NULL,
            dimension     TEXT    NOT NULL,  -- 'tier' | 'signal' | 'stat_type' | 'side'
            value         TEXT    NOT NULL,  -- e.g. 'ELITE', 'HOT_STREAK', 'hits'
            total         INTEGER NOT NULL,
            hits          INTEGER NOT NULL,
            hit_rate      REAL    NOT NULL,
            avg_ev        REAL,
            avg_score     REAL,
            sample_30d    INTEGER,           -- hits in last 30 days
            total_30d     INTEGER
        )
    """)

    # ── score_weights: learned weights for the vortex_score formula ───────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS score_weights (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at  TEXT NOT NULL,
            weight_key  TEXT NOT NULL UNIQUE,  -- e.g. 'tier_ELITE', 'signal_HOT_STREAK'
            weight      REAL NOT NULL,
            sample_size INTEGER,
            hit_rate    REAL
        )
    """)

    # ── active_board: today's live prop board (written by update_board.py) ────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_board (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name   TEXT    NOT NULL,
            sport         TEXT    NOT NULL,
            stat_type     TEXT    NOT NULL,
            line          REAL    NOT NULL,
            vortex_score  INTEGER NOT NULL,
            ev_percentage REAL    NOT NULL,
            case_summary  TEXT    NOT NULL,
            risk_summary  TEXT    NOT NULL,
            sportsbook    TEXT    NOT NULL,
            stats_json    TEXT    DEFAULT NULL,
            tier          TEXT    DEFAULT NULL
        )
    """)

    # Migrate existing DBs — safe to run repeatedly (ignores "already exists" errors)
    _new_cols = [
        "proj_edge REAL DEFAULT NULL",
        "damage_score INTEGER DEFAULT NULL",
        "stability_tier TEXT DEFAULT NULL",
        "lineup_spot INTEGER DEFAULT NULL",
        "commence_time TEXT DEFAULT NULL",
    ]
    for col_def in _new_cols:
        try:
            cur.execute(f"ALTER TABLE predictions ADD COLUMN {col_def}")
        except Exception:
            pass  # column already exists

    # ── moneyline_predictions: track moneyline picks for grading ─────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyline_predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at     TEXT    NOT NULL,
            game_date     TEXT    NOT NULL,
            game_pk       INTEGER,
            rec_team      TEXT    NOT NULL,
            opponent      TEXT    NOT NULL,
            odds          INTEGER NOT NULL,
            model_pct     REAL    NOT NULL,
            market_pct    REAL    NOT NULL,
            edge_pct      REAL    NOT NULL,
            confidence    REAL    NOT NULL,
            tier          TEXT    NOT NULL,
            rec_pitcher   TEXT,
            opp_pitcher   TEXT,
            rec_fip       REAL,
            opp_fip       REAL,
            park_factor   REAL,
            result        TEXT    DEFAULT NULL,
            actual_winner TEXT    DEFAULT NULL,
            graded_at     TEXT    DEFAULT NULL
        )
    """)

    conn.commit()
    conn.close()
    print("DB schema up to date.")

if __name__ == "__main__":
    init()
