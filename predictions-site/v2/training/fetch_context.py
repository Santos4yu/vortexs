"""
Offline, season-parametrized fetchers for VORTEX V2's Research-tab-derived
matchup/skill signals: opposing pitcher quality, opposing bullpen quality,
opposing team defense (OAA), a batter's own Statcast quality-of-contact
profile, a pitcher's own arsenal, a batter's own platoon (hand) splits, and
BvP history -- the same signal categories backend/analyze.py's grade_pick_v2
uses live, reconstructed here for arbitrary past seasons by threading
`season` through the same MLB Stats API / Baseball Savant calls
stats_mlb.py already makes (which hard-code the live SEASON constant).

LEAKAGE RULE (see dataset.py): every one of these is a FULL-SEASON aggregate,
not a per-game point-in-time cut -- that's inherent to how the underlying
Savant leaderboards and MLB season-stat endpoints work. To predict a game in
season S without leaking S's own outcome into its own feature, every
lookup here should be called with season=S-1 (the player's/team's prior-
season aggregate), never season=S itself. A rookie/new-to-the-league player
with no prior-season row gets {} back and dataset.py defaults it to 0.0 --
an accepted approximation, same category as stat_types.STANDARD_LINES.

Offline/training only -- never imported by any predictions-site/api/*.py
endpoint. Every function caches its raw result to disk forever (a finished
season's aggregate never changes), separate from stats_mlb's live TTL cache.
"""
import csv
import io
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import stats_mlb  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_SAVANT_HEADERS = {"User-Agent": "Mozilla/5.0 Vortex/1.0"}


def _cached_json(cache_name: str, fetch_fn):
    """Caches forever, EXCEPT it never persists a falsy result ({}/[]/None) --
    every fetch_fn() here swallows its own request exceptions and returns {}
    on failure, same as a genuine "no data for this player/season" result.
    Not caching the falsy case means a transient timeout gets retried on the
    next call instead of being permanently mistaken for confirmed absence
    (this bit fetch_gamelogs.py's season-roster cache once already -- see its
    own fix alongside this one)."""
    cache_file = DATA_DIR / f"{cache_name}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    result = fetch_fn()
    if result:
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    return result


# ── Opposing starter quality (ERA/FIP) ──────────────────────────────────────

def fetch_pitcher_season_quality(pitcher_id: int, season: int) -> dict:
    """{"era": float, "fip": float} for one pitcher's `season` line, or {}
    if the pitcher didn't pitch that season (call up, retired, etc.)."""
    def _fetch():
        data = stats_mlb._get(f"/people/{pitcher_id}/stats", {
            "stats": "season", "group": "pitching", "season": season, "sportId": 1,
        }, cache_key=None)
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0]["stat"]
        try:
            era = float(s.get("era"))
        except (TypeError, ValueError):
            return {}
        try:
            ip_raw = s.get("inningsPitched", "0.0")
            whole, frac = ip_raw.split(".")
            ip_dec = float(whole) + float(frac) / 3
            fip = round(
                (13 * int(s.get("homeRuns", 0)) + 3 * int(s.get("baseOnBalls", 0))
                 - 2 * int(s.get("strikeOuts", 0))) / max(ip_dec, 1) + 3.10, 2,
            )
        except Exception:
            fip = None
        return {"era": era, "fip": fip}

    return _cached_json(f"pitcher_quality_{pitcher_id}_{season}", _fetch)


# ── Opposing team batting quality (for pitcher-side rows) ───────────────────

def fetch_team_season_batting(team_id: int, season: int) -> dict:
    """{"ops": float, "runs_per_game": float} for `team_id` in `season` --
    what a pitcher's opposing LINEUP hit like that year (as opposed to OAA,
    which is the opposing DEFENSE and only matters for batter-side rows)."""
    def _fetch():
        data = stats_mlb._get(f"/teams/{team_id}/stats", {
            "stats": "season", "group": "hitting", "season": season,
        }, cache_key=None)
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0].get("stat", {})
        try:
            ops = float(s.get("ops"))
            games = int(s.get("gamesPlayed", 0) or 0)
            runs = int(s.get("runs", 0) or 0)
        except (TypeError, ValueError):
            return {}
        if games <= 0:
            return {}
        return {"ops": ops, "runs_per_game": round(runs / games, 2)}

    return _cached_json(f"team_batting_{team_id}_{season}", _fetch)


# ── Pitcher throwing hand (career-invariant -- no season param) ─────────────

def fetch_pitcher_hand(pitcher_id: int) -> str:
    """"L"/"R"/"?" -- a pitcher's throwing hand doesn't change across their
    career, so this caches once per pitcher_id, not per season."""
    def _fetch():
        data = stats_mlb._get(f"/people/{pitcher_id}", {}, cache_key=None)
        people = (data or {}).get("people") or [{}]
        return people[0].get("pitchHand", {}).get("code", "?")

    return _cached_json(f"pitcher_hand_{pitcher_id}", _fetch)


# ── Opposing bullpen quality ─────────────────────────────────────────────────

def fetch_team_bullpen_quality(team_id: int, season: int) -> dict:
    """Season-long relief-only ERA for `team_id` in `season` -- parametrized
    copy of backend/stats_mlb.py's get_team_bullpen(). {} if the sample is
    under 50 IP (bullpen "quality" is noise that early)."""
    def _fetch():
        data = stats_mlb._get(f"/teams/{team_id}/stats", {
            "stats": "statSplits", "group": "pitching", "season": season, "sitCodes": "rp",
        }, cache_key=None)
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0].get("stat", {})
        try:
            ip_raw = s.get("inningsPitched", "0.0")
            whole, frac = ip_raw.split(".")
            ip = float(whole) + float(frac) / 3
        except (ValueError, IndexError):
            ip = 0.0
        if ip < 50:
            return {}
        try:
            era = float(s.get("era"))
        except (TypeError, ValueError):
            return {}
        return {"era": era}

    return _cached_json(f"bullpen_quality_{team_id}_{season}", _fetch)


# ── Opposing team defense (Outs Above Average) ──────────────────────────────

def fetch_league_oaa(season: int) -> dict:
    """{team_id(str): total_oaa(int)} for the WHOLE league in `season`.

    NOTE: backend/stats_mlb.py's own get_team_defense_oaa() builds this URL
    with `type=Fielding&team=all` -- confirmed (2026-07) that URL now
    returns an HTML error page for EVERY year including the live season, so
    that signal is silently non-functional in the deployed Research tab
    today (a pre-existing bug, out of scope to fix there). `type=Fielder_Team`
    (broken down by position, summed here into one team total) is the
    working replacement found by testing Savant's leaderboard directly, but
    it has proven flaky under repeated calls in the same way -- treat this
    signal as best-effort; a lookup returning {} is not cached (see
    _cached_json) so it's retried rather than permanently zeroed."""
    def _fetch():
        try:
            resp = requests.get(
                "https://baseballsavant.mlb.com/leaderboard/outs_above_average",
                params={"type": "Fielder_Team", "year": season, "min": 1, "pos": "", "team": "", "csv": "true"},
                timeout=25, headers=_SAVANT_HEADERS,
            )
            resp.raise_for_status()
            table = {}
            for row in csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))):
                try:
                    tid = str(int(float(row.get("team_id") or 0)))
                    if tid == "0":
                        continue
                    table[tid] = table.get(tid, 0) + int(float(row.get("outs_above_average") or 0))
                except (ValueError, TypeError):
                    continue
            return table
        except Exception:
            return {}

    return _cached_json(f"league_oaa_{season}", _fetch)


# ── Batter's own Statcast quality-of-contact profile ────────────────────────

def fetch_league_batter_statcast(season: int) -> dict:
    """{player_id(str): {barrel_pct, hard_hit_pct, xwoba, whiff_pct}} for the
    WHOLE league in `season`, one call to Savant's "custom leaderboard"
    endpoint.

    NOTE: backend/stats_mlb.py's get_statcast_by_id() sources these same
    four fields from the `expected_statistics`/`plate-discipline` leaderboard
    endpoints -- confirmed (2026-07) that those two no longer return
    barrel/hard-hit/chase/whiff columns at all (checked both a 2023 and the
    live-season response; only ba/slg/woba-family columns come back now), so
    that signal is silently always-zero in the deployed Research tab today
    (a pre-existing bug, out of scope to fix there). `leaderboard/custom`
    with explicit `selections=` is the working replacement found by testing
    Savant directly; chase% has no reliable custom-leaderboard key ("
    o_swing_percent"/"chase_percent" both come back empty) so it's dropped
    from this feature set rather than shipping a column that's always 0."""
    def _sf(row: dict, key: str) -> float:
        v = str(row.get(key, "")).strip()
        if not v:
            return 0.0
        try:
            return float(v)
        except ValueError:
            return 0.0

    def _fetch():
        try:
            r = requests.get(
                "https://baseballsavant.mlb.com/leaderboard/custom",
                params={"type": "batter", "year": season, "min": "q",
                        "selections": "barrel_batted_rate,hard_hit_percent,xwoba,whiff_percent",
                        "chart": "false", "csv": "true"},
                headers=_SAVANT_HEADERS, timeout=25,
            )
            r.raise_for_status()
            all_data = {}
            for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
                pid = str(row.get("player_id", "")).strip()
                if not pid:
                    continue
                all_data[pid] = {
                    "barrel_pct": _sf(row, "barrel_batted_rate"),
                    "hard_hit_pct": _sf(row, "hard_hit_percent"),
                    "xwoba": _sf(row, "xwoba"),
                    "whiff_pct": _sf(row, "whiff_percent"),
                }
            return all_data
        except Exception:
            return {}

    return _cached_json(f"league_batter_statcast_{season}", _fetch)


# ── Pitcher's own arsenal ────────────────────────────────────────────────────

def fetch_pitcher_arsenal_summary(pitcher_id: int, season: int) -> dict:
    """Scalar summary of pitch mix (not the full per-pitch-type list, to
    avoid an open-ended categorical in the feature schema): {top_pitch_pct,
    arsenal_size} -- a one-pitch reliever vs. a deep-mix starter is the
    signal that matters for a fixed-width feature vector."""
    def _fetch():
        data = stats_mlb._get(f"/people/{pitcher_id}/stats", {
            "stats": "pitchArsenal", "season": season, "sportId": 1, "group": "pitching",
        }, cache_key=None)
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        pcts = [float(sp.get("stat", {}).get("percentage", 0) or 0) * 100 for sp in splits]
        if not pcts:
            return {}
        return {
            "top_pitch_pct": round(max(pcts), 1),
            "arsenal_size": sum(1 for p in pcts if p >= 5.0),
        }

    return _cached_json(f"pitcher_arsenal_{pitcher_id}_{season}", _fetch)


# ── Batter's own platoon (hand) splits ───────────────────────────────────────

def fetch_batter_hand_splits_season(batter_id: int, season: int) -> dict:
    """{"L": {avg, ops, pa, k_pct}, "R": {...}} for `season` -- parametrized
    copy of backend/stats_mlb.py's get_batter_hand_splits()."""
    def _fetch():
        result = {}
        for ph, site_code in (("L", "vl"), ("R", "vr")):
            data = stats_mlb._get(f"/people/{batter_id}/stats", {
                "stats": "statSplits", "group": "hitting", "season": season,
                "sportId": 1, "sitCodes": site_code,
            }, cache_key=None)
            splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
            if not splits:
                continue
            s = splits[0].get("stat", {})
            pa = int(s.get("plateAppearances", 0) or 0)
            so = int(s.get("strikeOuts", 0) or 0)
            try:
                ops = float(s.get("ops", 0) or 0)
            except (TypeError, ValueError):
                ops = 0.0
            try:
                avg = float(s.get("avg", 0) or 0)
            except (TypeError, ValueError):
                avg = 0.0
            result[ph] = {
                "avg": avg, "ops": ops, "pa": pa,
                "k_pct": round(so / pa * 100, 1) if pa else 0.0,
            }
        return result

    return _cached_json(f"batter_hand_splits_{batter_id}_{season}", _fetch)


# ── BvP (career-to-date through a target season) ────────────────────────────

def fetch_bvp_through_season(batter_id: int, pitcher_id: int, through_season: int) -> dict:
    """Career head-to-head totals between this exact batter/pitcher pair,
    restricted to seasons <= through_season (so a 2023 training row can't see
    2024/2025 at-bats against the same pitcher). One call covers this pair's
    entire career at once (MLB's vsPlayer split isn't season-filterable), cached
    forever per pair and re-aggregated locally for each through_season."""
    def _fetch():
        data = stats_mlb._get(f"/people/{batter_id}/stats", {
            "stats": "vsPlayer", "group": "hitting",
            "opposingPlayerId": pitcher_id, "sportId": 1,
        }, cache_key=None)
        return ((data or {}).get("stats") or [{}])[0].get("splits", [])

    splits = _cached_json(f"bvp_{batter_id}_vs_{pitcher_id}", _fetch)
    ab = hits = k = bb = tb = pa = 0
    for sp in splits:
        try:
            season_num = int(sp.get("season", 0))
        except (TypeError, ValueError):
            continue
        if season_num > through_season:
            continue
        s = sp.get("stat", {})
        ab += int(s.get("atBats", 0) or 0)
        hits += int(s.get("hits", 0) or 0)
        k += int(s.get("strikeOuts", 0) or 0)
        bb += int(s.get("baseOnBalls", 0) or 0)
        tb += int(s.get("totalBases", 0) or 0)
        pa += int(s.get("plateAppearances", 0) or 0)
    if ab == 0 and pa == 0:
        return {}
    return {
        "avg": round(hits / ab, 3) if ab else 0.0,
        "ops": round((tb / ab if ab else 0.0) + ((hits + bb) / pa if pa else 0.0), 3),
        "pa": pa,
    }
