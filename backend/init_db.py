"""
Initialize / migrate the VORTEX database schema.
Run once to add new tables — safe to re-run (uses IF NOT EXISTS).
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "vortex.db"


def _table_exists(cur, name: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None

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
            graded_at      TEXT   DEFAULT NULL,
            matchup_score  REAL   DEFAULT NULL,
            matchup_label  TEXT   DEFAULT NULL,
            case_summary   TEXT   DEFAULT NULL,
            risk_summary   TEXT   DEFAULT NULL
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

    # Bot-only Home Run Sniper evaluation history. Every priced, confirmed
    # candidate is retained for calibration, including PASS classifications.
    cur.execute("""
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
        "matchup_score REAL DEFAULT NULL",
        "matchup_label TEXT DEFAULT NULL",
        "case_summary TEXT DEFAULT NULL",
        "risk_summary TEXT DEFAULT NULL",
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
            model_version TEXT    DEFAULT 'legacy',
            market_event_id TEXT,
            sportsbook    TEXT,
            market_updated_at TEXT,
            raw_model_pct REAL,
            reliability   REAL,
            expected_value REAL,
            factor_json   TEXT,
            decision_at   TEXT,
            result        TEXT    DEFAULT NULL,
            actual_winner TEXT    DEFAULT NULL,
            graded_at     TEXT    DEFAULT NULL
        )
    """)
    for col_def in (
        "model_version TEXT DEFAULT 'legacy'", "market_event_id TEXT", "sportsbook TEXT",
        "market_updated_at TEXT", "raw_model_pct REAL", "reliability REAL",
        "expected_value REAL", "factor_json TEXT", "decision_at TEXT",
    ):
        try:
            cur.execute(f"ALTER TABLE moneyline_predictions ADD COLUMN {col_def}")
        except Exception:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyline_model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL, game_date TEXT NOT NULL, game_pk INTEGER NOT NULL,
            model_version TEXT NOT NULL, market_event_id TEXT,
            home_team TEXT NOT NULL, away_team TEXT NOT NULL,
            home_model_pct REAL NOT NULL, home_market_pct REAL NOT NULL,
            home_odds INTEGER, away_odds INTEGER, reliability REAL,
            lineups_confirmed INTEGER NOT NULL, tier TEXT NOT NULL,
            actual_home_win INTEGER DEFAULT NULL, actual_winner TEXT DEFAULT NULL,
            graded_at TEXT DEFAULT NULL
        )
    """)

    # ── nrfi_predictions: track NRFI/YRFI picks for grading ────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nrfi_predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at     TEXT    NOT NULL,
            game_date     TEXT    NOT NULL,
            game_pk       INTEGER,
            home_abbr     TEXT    NOT NULL,
            away_abbr     TEXT    NOT NULL,
            home_pitcher  TEXT,
            away_pitcher  TEXT,
            recommendation TEXT   NOT NULL,
            confidence    TEXT    NOT NULL,
            score         INTEGER NOT NULL,
            confidence_pct REAL   NOT NULL,
            result        TEXT    DEFAULT NULL,
            actual_result TEXT    DEFAULT NULL,
            graded_at     TEXT    DEFAULT NULL
        )
    """)

    # The previous WNBA model was retired. Remove its board and prediction
    # artifacts so the replacement model starts with a clean calibration set.
    # Some fresh deployments never had the legacy props_board table, so this
    # cleanup must not prevent the new schema or Discord commands from loading.
    if _table_exists(cur, "props_board"):
        cur.execute("DELETE FROM props_board WHERE upper(sport)='WNBA'")
    if _table_exists(cur, "predictions"):
        cur.execute("DELETE FROM predictions WHERE upper(sport)='WNBA'")

    # Independent WNBA model: evaluated candidates and official selections are
    # never stored in the MLB props_board/predictions tables.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wnba_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluated_at TEXT NOT NULL, model_version TEXT NOT NULL,
            game_date TEXT NOT NULL, commence_time TEXT NOT NULL,
            player_id TEXT NOT NULL, player_name TEXT NOT NULL,
            team TEXT NOT NULL, opponent TEXT NOT NULL,
            market_key TEXT NOT NULL, prop_type TEXT NOT NULL,
            side TEXT NOT NULL, line REAL NOT NULL,
            over_odds INTEGER, under_odds INTEGER, best_book TEXT,
            projected_minutes REAL NOT NULL, projected_mean REAL NOT NULL,
            projected_floor REAL, projected_ceiling REAL, standard_deviation REAL,
            selected_probability REAL NOT NULL, market_probability REAL,
            edge_pp REAL, fair_odds INTEGER, data_quality INTEGER NOT NULL,
            variance_score INTEGER NOT NULL, variance_label TEXT NOT NULL,
            tier TEXT NOT NULL, publish INTEGER NOT NULL, watchlist INTEGER NOT NULL,
            reasons_json TEXT NOT NULL, risks_json TEXT NOT NULL,
            rejection_json TEXT NOT NULL, inputs_json TEXT NOT NULL,
            UNIQUE(game_date, player_id, market_key, line, side, model_version)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wnba_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL, model_version TEXT NOT NULL,
            evaluation_id INTEGER, game_date TEXT NOT NULL,
            commence_time TEXT NOT NULL, player_id TEXT NOT NULL,
            player_name TEXT NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
            market_key TEXT NOT NULL, prop_type TEXT NOT NULL,
            side TEXT NOT NULL, line REAL NOT NULL,
            selected_probability REAL NOT NULL, market_probability REAL,
            edge_pp REAL, fair_odds INTEGER, best_book TEXT, best_odds INTEGER,
            over_odds INTEGER, under_odds INTEGER,
            data_quality INTEGER NOT NULL, variance_label TEXT NOT NULL,
            tier TEXT NOT NULL, result TEXT, actual_value REAL, actual_minutes REAL,
            closing_line REAL, closing_odds INTEGER, graded_at TEXT,
            FOREIGN KEY(evaluation_id) REFERENCES wnba_evaluations(id),
            UNIQUE(game_date, player_id, market_key, line, side, model_version)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wnba_eval_day ON wnba_evaluations(game_date, tier)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wnba_pred_day ON wnba_predictions(game_date, result)")
    try:
        cur.execute("ALTER TABLE wnba_predictions ADD COLUMN best_odds INTEGER")
    except sqlite3.OperationalError:
        pass
    cur.execute("""CREATE TABLE IF NOT EXISTS wnba_odds_budget (
        budget_day TEXT PRIMARY KEY, credits_used INTEGER NOT NULL DEFAULT 0
    )""")

    conn.commit()
    conn.close()
    print("DB schema up to date.")

if __name__ == "__main__":
    init()
