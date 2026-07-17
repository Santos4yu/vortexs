"""
VORTEX Result Grader
====================
Pulls actual game stats for yesterday's predictions and grades each one
hit / miss / push. Updates signal_accuracy and score_weights tables.

Run nightly after games finish (~midnight ET).
"""
import json
import logging
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import stats_mlb

log = logging.getLogger(__name__)


def _norm_name(s: str) -> str:
    """Accent/suffix-insensitive name key so 'Julio Rodríguez' == 'Julio Rodriguez'
    and 'Luis Garcia Jr.' == 'Luis Garcia'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))   # drop accents
    s = s.lower().strip().replace(".", "")
    for suf in (" jr", " sr", " ii", " iii", " iv"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    return s
logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(message)s")

DB_PATH   = Path(__file__).resolve().parent.parent / "vortex.db"
BASE_MLB  = stats_mlb.BASE
BASE_NBA  = "https://stats.nba.com/stats"
TIMEOUT   = 15
DELAY     = 0.3

# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── MLB result fetcher ────────────────────────────────────────────────────────

def _mlb_game_stats(game_date: str) -> dict[str, dict]:
    """
    Returns {player_name: {stat_key: value, "_final": bool}} for every player with
    a recorded plate appearance / inning on game_date — including games still in
    progress. The "_final" flag lets the grader lock decided picks live (an Over
    the moment it clears, an Under the moment it busts) and leave the rest pending.
    """
    # Step 1: get game PKs for the date (Final OR Live)
    try:
        r = requests.get(f"{BASE_MLB}/schedule",
                         params={"sportId": 1, "date": game_date}, timeout=TIMEOUT)
        r.raise_for_status()
        sched = r.json()
    except Exception as e:
        log.error("MLB schedule fetch failed: %s", e)
        return {}

    games = []  # (game_pk, is_final)
    for date_entry in sched.get("dates", []):
        for game in date_entry.get("games", []):
            state = game.get("status", {}).get("abstractGameState", "")
            if state == "Final":
                games.append((game["gamePk"], True))
            elif state == "Live":
                games.append((game["gamePk"], False))

    if not games:
        log.info("No Final/Live MLB games found for %s", game_date)
        return {}

    # Step 2: fetch each game's boxscore individually
    results = {}
    for pk, is_final in games:
        time.sleep(DELAY)
        try:
            bs_r = requests.get(f"{BASE_MLB}/game/{pk}/boxscore", timeout=TIMEOUT)
            bs_r.raise_for_status()
            bs = bs_r.json()
        except Exception as e:
            log.warning("MLB boxscore fetch failed for gamePk=%s: %s", pk, e)
            continue

        for side in ("home", "away"):
            team = bs.get("teams", {}).get(side, {})
            for pid, pdata in team.get("players", {}).items():
                pname = pdata.get("person", {}).get("fullName", "")
                if not pname:
                    continue
                s      = pdata.get("stats", {})
                bat    = s.get("batting", {})
                pit    = s.get("pitching", {})
                played = bool(bat.get("atBats") or pit.get("outs") or pit.get("battersFaced"))
                # A player on the roster who never took an at-bat / threw a pitch is a
                # DNP. Keep them (flagged) so the grader can VOID instead of stranding
                # the pick. A real stat line always wins over a DNP entry for the name.
                if pname in results and not results[pname].get("_dnp", True) and not played:
                    continue
                h  = int(bat.get("hits", 0))
                d  = int(bat.get("doubles", 0))
                t  = int(bat.get("triples", 0))
                hr = int(bat.get("homeRuns", 0))
                r  = int(bat.get("runs", 0))
                rbi = int(bat.get("rbi", 0))
                bb  = int(bat.get("baseOnBalls", 0))
                hbp = int(bat.get("hitByPitch", 0))
                sb  = int(bat.get("stolenBases", 0))
                singles = max(0, h - d - t - hr)
                fantasy_score = (singles * 3 + d * 5 + t * 8 + hr * 10
                                 + r * 2 + rbi * 2 + bb * 2 + hbp * 2 + sb * 5)
                results[pname] = {
                    "hits":           h,
                    "runs":           r,
                    "rbi":            rbi,
                    "total_bases":    int(bat.get("totalBases", 0)),
                    "home_runs":      hr,
                    "strikeouts_bat": int(bat.get("strikeOuts", 0)),
                    "walks":          bb,
                    "hits_runs_rbis": h + r + rbi,
                    "fantasy_score":  fantasy_score,
                    "strikeouts_pit": int(pit.get("strikeOuts", 0)),
                    "outs_pit":       int(pit.get("outs", 0)),
                    "hits_allowed_pit": int(pit.get("hits", 0)),
                    "earned_runs_pit":  int(pit.get("earnedRuns", 0)),
                    "_final":         is_final,
                    "_dnp":           not played,
                }

    n_final = sum(1 for _, f in games if f)
    log.info("MLB stats: %d players for %s (%d games, %d final, %d live)",
             len(results), game_date, len(games), n_final, len(games) - n_final)
    return results


def _nba_game_stats(game_date: str) -> dict[str, dict]:
    """
    Returns {player_name: {stat_key: value}} for NBA games on game_date.
    """
    url = "https://stats.nba.com/stats/scoreboardv2"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://www.nba.com/",
        "Accept":     "application/json",
    }
    params = {"GameDate": game_date, "LeagueID": "00", "DayOffset": "0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("NBA scoreboard fetch failed: %s", e)
        return {}

    game_ids = []
    for rs in data.get("resultSets", []):
        if rs.get("name") == "GameHeader":
            idx = {h: i for i, h in enumerate(rs["headers"])}
            for row in rs.get("rowSet", []):
                game_ids.append(row[idx["GAME_ID"]])

    results = {}
    for gid in game_ids:
        time.sleep(DELAY)
        try:
            r = requests.get(
                "https://stats.nba.com/stats/boxscoretraditionalv2",
                params={"GameID": gid, "StartPeriod": 0, "EndPeriod": 10,
                        "StartRange": 0, "EndRange": 0, "RangeType": 0},
                headers=headers, timeout=TIMEOUT,
            )
            r.raise_for_status()
            box = r.json()
        except Exception as e:
            log.warning("NBA boxscore failed for %s: %s", gid, e)
            continue

        for rs in box.get("resultSets", []):
            if rs.get("name") == "PlayerStats":
                idx = {h: i for i, h in enumerate(rs["headers"])}
                for row in rs.get("rowSet", []):
                    name = row[idx["PLAYER_NAME"]]
                    results[name] = {
                        "points":    row[idx["PTS"]] or 0,
                        "rebounds":  row[idx["REB"]] or 0,
                        "assists":   row[idx["AST"]] or 0,
                        "threes":    row[idx["FG3M"]] or 0,
                        "blocks":    row[idx["BLK"]] or 0,
                        "steals":    row[idx["STL"]] or 0,
                        "pts_reb_ast": (row[idx["PTS"]] or 0) + (row[idx["REB"]] or 0) + (row[idx["AST"]] or 0),
                    }
    log.info("NBA boxscore: %d players fetched for %s", len(results), game_date)
    return results


# ── stat key resolver ─────────────────────────────────────────────────────────

MLB_STAT_KEY = {
    "batter_hits":           "hits",
    "batter_total_bases":    "total_bases",
    "batter_home_runs":      "home_runs",
    "batter_rbis":           "rbi",
    "batter_runs_scored":    "runs",
    "pitcher_strikeouts":    "strikeouts_pit",
    "batter_hits_runs_rbis": "hits_runs_rbis",
    "batter_fantasy_score":  "fantasy_score",
    "batter_walks":          "walks",
    "pitcher_outs":          "outs_pit",
    "pitcher_hits_allowed":  "hits_allowed_pit",
    "pitcher_earned_runs":   "earned_runs_pit",
}

# Fallback: map display stat_type names when market_key is missing
MLB_STAT_TYPE_KEY = {
    "Hits":              "hits",
    "Total Bases":       "total_bases",
    "Home Runs":         "home_runs",
    "RBIs":              "rbi",
    "Runs Scored":       "runs",
    "Strikeouts":        "strikeouts_pit",
    "Hits+Runs+RBIs":    "hits_runs_rbis",
    "Fantasy Score (PP)": "fantasy_score",
    "Walks":             "walks",
    "Outs":              "outs_pit",
    "Hits Allowed":      "hits_allowed_pit",
    "Earned Runs":       "earned_runs_pit",
    "Earned Runs Allowed": "earned_runs_pit",
}

NBA_STAT_KEY = {
    "points":    "points",
    "rebounds":  "rebounds",
    "assists":   "assists",
    "threes":    "threes",
    "blocks":    "blocks",
    "steals":    "steals",
    "pts_reb_ast": "pts_reb_ast",
}


def _grade(actual: float, line: float, side: str) -> str:
    if side == "over":
        if actual > line:  return "hit"
        if actual == line: return "push"
        return "miss"
    else:
        if actual < line:  return "hit"
        if actual == line: return "push"
        return "miss"


# ── main grader ───────────────────────────────────────────────────────────────

def grade_date(game_date: str) -> dict:
    """Grade all ungraded predictions for game_date. Returns summary dict."""
    conn = _db()
    cur  = conn.cursor()

    ungraded = cur.execute(
        "SELECT * FROM predictions WHERE game_date=? AND result IS NULL",
        (game_date,)
    ).fetchall()

    if not ungraded:
        log.info("No ungraded predictions for %s", game_date)
        conn.close()
        return {"pending": 0, "graded": 0, "unresolved": 0, "mlb_players": 0}

    log.info("%d ungraded predictions for %s", len(ungraded), game_date)

    # Fetch actual stats (Final + Live games). The betting day is now anchored to
    # 4 AM Mountain, so picks are always labeled with the date their games belong
    # to — no cross-day fallback needed.
    sports = {r["sport"] for r in ungraded}
    mlb_stats = _mlb_game_stats(game_date) if "MLB" in sports else {}
    nba_stats = _nba_game_stats(game_date) if "NBA" in sports else {}

    # Accent/suffix-insensitive fallback indexes (Julio Rodríguez → julio rodriguez)
    mlb_norm = {_norm_name(k): v for k, v in mlb_stats.items()}
    nba_norm = {_norm_name(k): v for k, v in nba_stats.items()}

    graded_at = datetime.now(timezone.utc).isoformat()
    graded = 0
    voided = 0
    missed_lookup = 0
    unresolved_players = []

    for row in ungraded:
        sport      = row["sport"]
        player     = row["player_name"]
        market_key = row["market_key"]
        line       = row["line"]
        side       = row["side"]

        if sport == "MLB":
            player_stats = mlb_stats.get(player) or mlb_norm.get(_norm_name(player)) or {}
            stat_key     = MLB_STAT_KEY.get(market_key) or MLB_STAT_TYPE_KEY.get(row["stat_type"])
        else:
            player_stats = nba_stats.get(player) or nba_norm.get(_norm_name(player)) or {}
            stat_key     = NBA_STAT_KEY.get(market_key)

        if not player_stats or not stat_key:
            missed_lookup += 1
            unresolved_players.append(f"{player} ({market_key or row['stat_type']})")
            continue

        is_final = bool(player_stats.get("_final", True))   # NBA path → treat as final

        # Player was on the roster but never played → VOID (refund), not a loss.
        # While the game is live they could still enter, so only void once it's final.
        if player_stats.get("_dnp"):
            if is_final:
                cur.execute(
                    "UPDATE predictions SET result='void', actual_value=NULL, graded_at=? WHERE id=?",
                    (graded_at, row["id"]))
                voided += 1
            continue

        actual = player_stats.get(stat_key)
        if actual is None:
            missed_lookup += 1
            unresolved_players.append(f"{player} (no {stat_key})")
            continue

        actual_f, line_f = float(actual), float(line)
        if is_final:
            result = _grade(actual_f, line_f, side)
        elif side == "over" and actual_f > line_f:
            result = "hit"      # Over cleared mid-game — locks (counts only rise)
        elif side == "under" and actual_f > line_f:
            result = "miss"     # Under busted mid-game — locks (counts only rise)
        else:
            result = None       # live but not yet decided — leave pending
        if result is None:
            continue

        cur.execute(
            "UPDATE predictions SET result=?, actual_value=?, graded_at=? WHERE id=?",
            (result, actual, graded_at, row["id"])
        )
        graded += 1

    conn.commit()
    log.info("Graded %d/%d predictions (%d void, %d unresolved)",
             graded, len(ungraded), voided, missed_lookup)
    if unresolved_players:
        log.info("Unresolved: %s", ", ".join(unresolved_players[:10]))

    # Rebuild signal accuracy after grading
    if graded or voided:
        _rebuild_accuracy(conn)
        _rebuild_weights(conn)
    conn.close()

    # Also grade moneyline predictions for this date
    try:
        grade_moneyline_date(game_date)
    except Exception as e:
        log.warning("Moneyline grading failed: %s", e)

    # Also grade NRFI predictions for this date
    try:
        grade_nrfi_date(game_date)
    except Exception as e:
        log.warning("NRFI grading failed: %s", e)

    return {
        "pending": len(ungraded),
        "graded": graded,
        "voided": voided,
        "unresolved": missed_lookup,
        "mlb_players": len(mlb_stats),
        "unresolved_list": unresolved_players[:5],
    }


# ── accuracy aggregator ───────────────────────────────────────────────────────

def _rebuild_accuracy(conn: sqlite3.Connection):
    """Recompute hit rates for every dimension and store in signal_accuracy."""
    cur = conn.cursor()
    cur.execute("DELETE FROM signal_accuracy")

    now = datetime.now(timezone.utc).isoformat()
    from datetime import datetime as _dt
    _ET_OFF = timezone(timedelta(hours=-4))
    cutoff_30d = (_dt.now(_ET_OFF).date() - timedelta(days=30)).isoformat()

    dimensions = [
        ("tier",      "tier"),
        ("signal",    "signal_type"),
        ("stat_type", "stat_type"),
        ("side",      "side"),
        ("sport",     "sport"),
        ("book",      "best_book"),
    ]

    for dim_name, col in dimensions:
        rows = cur.execute(f"""
            SELECT sport, {col} as val,
                   COUNT(*) as total,
                   SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as hits,
                   AVG(ev_percentage) as avg_ev,
                   AVG(vortex_score)  as avg_score
            FROM predictions
            WHERE result IS NOT NULL AND result NOT IN ('push','void') AND {col} IS NOT NULL
              AND stat_type != 'Home Runs'
            GROUP BY sport, {col}
        """).fetchall()

        for r in rows:
            total   = r["total"]
            hits    = r["hits"]
            if total < 5:
                continue
            hit_rate = round(hits / total, 4)

            # Last 30 days
            r30 = cur.execute(f"""
                SELECT COUNT(*) as t, SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as h
                FROM predictions
                WHERE result IS NOT NULL AND result NOT IN ('push','void')
                  AND stat_type != 'Home Runs'
                  AND {col}=? AND sport=? AND game_date >= ?
            """, (r["val"], r["sport"], cutoff_30d)).fetchone()

            cur.execute("""
                INSERT INTO signal_accuracy
                  (updated_at, sport, dimension, value, total, hits, hit_rate,
                   avg_ev, avg_score, sample_30d, total_30d)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (now, r["sport"], dim_name, r["val"], total, hits, hit_rate,
                  r["avg_ev"], r["avg_score"], r30["h"] if r30 else None,
                  r30["t"] if r30 else None))

    conn.commit()
    log.info("Signal accuracy rebuilt.")


# ── weight learner ────────────────────────────────────────────────────────────

def _rebuild_weights(conn: sqlite3.Connection):
    """
    Update score_weights based on observed hit rates.
    Only updates weights when sample >= 20.
    """
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    rows = cur.execute("""
        SELECT dimension, value, hit_rate, total
        FROM signal_accuracy
        WHERE total >= 20
    """).fetchall()

    for r in rows:
        key      = f"{r['dimension']}_{r['value']}"
        hit_rate = r["hit_rate"]
        # Weight = how much better than 50% baseline (normalized 0-2)
        weight   = round(hit_rate / 0.50, 4)

        cur.execute("""
            INSERT INTO score_weights (updated_at, weight_key, weight, sample_size, hit_rate)
            VALUES (?,?,?,?,?)
            ON CONFLICT(weight_key) DO UPDATE SET
                updated_at  = excluded.updated_at,
                weight      = excluded.weight,
                sample_size = excluded.sample_size,
                hit_rate    = excluded.hit_rate
        """, (now, key, weight, r["total"], hit_rate))

    conn.commit()
    log.info("Score weights updated for %d signals.", len(rows))


# ── Moneyline grading ────────────────────────────────────────────────────────

def grade_moneyline_date(game_date: str) -> dict:
    """Grade all ungraded moneyline predictions for game_date."""
    conn = _db()
    cur = conn.cursor()

    # Ensure table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyline_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT, game_date TEXT, game_pk INTEGER,
            rec_team TEXT, opponent TEXT, odds INTEGER,
            model_pct REAL, market_pct REAL, edge_pct REAL,
            confidence REAL, tier TEXT,
            rec_pitcher TEXT, opp_pitcher TEXT,
            rec_fip REAL, opp_fip REAL, park_factor REAL,
            result TEXT DEFAULT NULL, actual_winner TEXT DEFAULT NULL,
            graded_at TEXT DEFAULT NULL
        )
    """)

    ungraded = cur.execute(
        "SELECT * FROM moneyline_predictions WHERE game_date=? AND result IS NULL",
        (game_date,)
    ).fetchall()

    if not ungraded:
        log.info("No ungraded moneyline predictions for %s", game_date)
        conn.close()
        return {"pending": 0, "graded": 0}

    log.info("%d ungraded moneyline predictions for %s", len(ungraded), game_date)

    # Fetch final scores for this date
    data = stats_mlb._get("/schedule", {
        "sportId": 1, "date": game_date, "hydrate": "linescore",
    }, cache_key=f"ml_results_{game_date}")

    # Build game results: {game_pk: winner_team_name}
    game_results: dict[int, str] = {}
    for dt in (data or {}).get("dates", []):
        for g in dt.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            pk = g.get("gamePk")
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_r = home.get("score", 0) or 0
            away_r = away.get("score", 0) or 0
            if home_r > away_r:
                game_results[pk] = (home.get("team", {}).get("name", ""), "home")
            elif away_r > home_r:
                game_results[pk] = (away.get("team", {}).get("name", ""), "away")
            else:
                game_results[pk] = ("", "tbd")

    graded = 0
    graded_at = datetime.now(timezone.utc).isoformat()

    for row in ungraded:
        pk = row["game_pk"]
        if pk not in game_results:
            continue

        winner, side = game_results[pk]
        if not winner:
            continue

        rec_team = row["rec_team"]
        if winner == rec_team:
            result = "hit"
        else:
            result = "miss"

        cur.execute(
            "UPDATE moneyline_predictions SET result=?, actual_winner=?, graded_at=? WHERE id=?",
            (result, winner, graded_at, row["id"])
        )
        graded += 1

    conn.commit()
    log.info("Graded %d/%d moneyline predictions", graded, len(ungraded))
    conn.close()
    return {"pending": len(ungraded), "graded": graded}


def get_moneyline_accuracy() -> dict:
    """Return moneyline prediction accuracy stats."""
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM moneyline_predictions WHERE result IS NOT NULL").fetchone()[0]
        hits = conn.execute("SELECT COUNT(*) FROM moneyline_predictions WHERE result='hit'").fetchone()[0]
        misses = conn.execute("SELECT COUNT(*) FROM moneyline_predictions WHERE result='miss'").fetchone()[0]

        # By tier
        tiers = {}
        for tier in ("NOTABLE", "MODEST", "SLIGHT"):
            t = conn.execute(
                "SELECT COUNT(*) FROM moneyline_predictions WHERE tier=? AND result IS NOT NULL", (tier,)
            ).fetchone()[0]
            h = conn.execute(
                "SELECT COUNT(*) FROM moneyline_predictions WHERE tier=? AND result='hit'", (tier,)
            ).fetchone()[0]
            if t > 0:
                tiers[tier] = {"total": t, "hits": h, "rate": round(h / t * 100, 1)}

        # Recent (last 7 days)
        recent = conn.execute("""
            SELECT COUNT(*) FROM moneyline_predictions
            WHERE result IS NOT NULL AND game_date >= date('now', '-7 days')
        """).fetchone()[0]
        recent_hits = conn.execute("""
            SELECT COUNT(*) FROM moneyline_predictions
            WHERE result='hit' AND game_date >= date('now', '-7 days')
        """).fetchone()[0]
    finally:
        conn.close()

    return {
        "total": total,
        "hits": hits,
        "misses": misses,
        "rate": round(hits / total * 100, 1) if total else 0,
        "tiers": tiers,
        "recent_total": recent,
        "recent_hits": recent_hits,
        "recent_rate": round(recent_hits / recent * 100, 1) if recent else 0,
    }


# ── NRFI grading ─────────────────────────────────────────────────────────────

def grade_nrfi_date(game_date: str) -> dict:
    """Grade all ungraded NRFI predictions for game_date."""
    conn = _db()
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

    ungraded = cur.execute(
        "SELECT * FROM nrfi_predictions WHERE game_date=? AND result IS NULL",
        (game_date,)
    ).fetchall()

    if not ungraded:
        log.info("No ungraded NRFI predictions for %s", game_date)
        conn.close()
        return {"pending": 0, "graded": 0}

    log.info("%d ungraded NRFI predictions for %s", len(ungraded), game_date)

    # Fetch final scores for this date
    data = stats_mlb._get("/schedule", {
        "sportId": 1, "date": game_date, "hydrate": "linescore",
    }, cache_key=f"nrfi_results_{game_date}")

    # Build game results: {game_pk: {"home_r": int, "away_r": int}}
    game_results: dict[int, dict] = {}
    for dt in (data or {}).get("dates", []):
        for g in dt.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            pk = g.get("gamePk")
            teams = g.get("teams", {})
            home_r = teams.get("home", {}).get("score", 0) or 0
            away_r = teams.get("away", {}).get("score", 0) or 0
            game_results[pk] = {"home_r": home_r, "away_r": away_r}

    graded = 0
    graded_at = datetime.now(timezone.utc).isoformat()

    for row in ungraded:
        pk = row["game_pk"]
        if pk not in game_results:
            continue

        gr = game_results[pk]
        home_r = gr["home_r"]
        away_r = gr["away_r"]
        first_inning_runs = home_r + away_r  # simplified: check total (1st inning not always in linescore)
        actual = "NRFI" if first_inning_runs == 0 else "YRFI"

        rec = row["recommendation"]
        if rec == actual:
            result = "hit"
        else:
            result = "miss"

        cur.execute(
            "UPDATE nrfi_predictions SET result=?, actual_result=?, graded_at=? WHERE id=?",
            (result, actual, graded_at, row["id"])
        )
        graded += 1

    conn.commit()
    log.info("Graded %d/%d NRFI predictions", graded, len(ungraded))
    conn.close()
    return {"pending": len(ungraded), "graded": graded}


def get_nrfi_accuracy() -> dict:
    """Return NRFI prediction accuracy stats."""
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM nrfi_predictions WHERE result IS NOT NULL").fetchone()[0]
        hits = conn.execute("SELECT COUNT(*) FROM nrfi_predictions WHERE result='hit'").fetchone()[0]

        # By confidence
        confs = {}
        for conf in ("STRONG", "LEAN"):
            t = conn.execute(
                "SELECT COUNT(*) FROM nrfi_predictions WHERE confidence=? AND result IS NOT NULL", (conf,)
            ).fetchone()[0]
            h = conn.execute(
                "SELECT COUNT(*) FROM nrfi_predictions WHERE confidence=? AND result='hit'", (conf,)
            ).fetchone()[0]
            if t > 0:
                confs[conf] = {"total": t, "hits": h, "rate": round(h / t * 100, 1)}

        # By rec type
        recs = {}
        for rec in ("NRFI", "YRFI"):
            t = conn.execute(
                "SELECT COUNT(*) FROM nrfi_predictions WHERE recommendation=? AND result IS NOT NULL", (rec,)
            ).fetchone()[0]
            h = conn.execute(
                "SELECT COUNT(*) FROM nrfi_predictions WHERE recommendation=? AND result='hit'", (rec,)
            ).fetchone()[0]
            if t > 0:
                recs[rec] = {"total": t, "hits": h, "rate": round(h / t * 100, 1)}
    finally:
        conn.close()

    return {
        "total": total,
        "hits": hits,
        "rate": round(hits / total * 100, 1) if total else 0,
        "confidences": confs,
        "recommendations": recs,
    }


def get_all_results(date_str: str) -> dict:
    """Get all moneyline + NRFI results for a date."""
    conn = _db()
    try:
        ml_rows = conn.execute(
            "SELECT * FROM moneyline_predictions WHERE game_date=?", (date_str,)
        ).fetchall()
        nrfi_rows = conn.execute(
            "SELECT * FROM nrfi_predictions WHERE game_date=?", (date_str,)
        ).fetchall()
    finally:
        conn.close()
    return {"moneyline": ml_rows, "nrfi": nrfi_rows}


# ── summary printer ───────────────────────────────────────────────────────────

def print_accuracy_report():
    conn = _db()
    rows = conn.execute("""
        SELECT sport, dimension, value, total, hits, hit_rate, avg_ev
        FROM signal_accuracy
        ORDER BY sport, dimension, hit_rate DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No accuracy data yet — need graded predictions first.")
        return

    current = None
    for r in rows:
        header = f"{r['sport']} / {r['dimension']}"
        if header != current:
            print(f"\n{'─'*50}")
            print(f"  {header}")
            current = header
        bar = "█" * int(r["hit_rate"] * 20)
        print(f"  {r['value']:<20} {r['hit_rate']*100:5.1f}%  {bar}  ({r['hits']}/{r['total']})  avg EV {r['avg_ev']:+.1f}%")


if __name__ == "__main__":
    import sys
    # Windows consoles default to cp1252, which can't encode the box-drawing /
    # bar chars in print_accuracy_report(). Force UTF-8 so manual grading and
    # verification runs don't crash on the summary.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt
    target_date = sys.argv[1] if len(sys.argv) > 1 else \
                  (_dt.now(_ZI("America/New_York")).date() - timedelta(days=1)).isoformat()
    print(f"Grading predictions for {target_date}...")
    grade_date(target_date)
    print_accuracy_report()
