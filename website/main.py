"""VORTEX Web — FastAPI server with Discord OAuth + all bot features."""

import os
import sys
import json
import asyncio
import secrets
import logging
import sqlite3
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import stats_mlb
import update_board
import nrfi as nrfi_module
import vortextime
from research import get_research_card, fuzzy_search

from website import auth as oauth

# ── Board helpers ────────────────────────────────────────────────────────────

DB_PATH = ROOT / "vortex.db"

_LIVE_FILTER = (
    "(commence_time IS NULL OR commence_time = '' "
    "OR commence_time > strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
)
_ALLOWED_MLB = ("hits+runs", "hrr", "total bases", "hits",
                "strikeout", "fantasy", "outs", "hits allowed", "earned runs")


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_board(sport=None, tier=None, stat_filter=None, limit=None):
    conn = _db()
    _tier_gate = "tier IN ('ELITE','STRONG')"
    # Try props_board first (written by update_board.py), fall back to active_board
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    table = "props_board" if "props_board" in tables else "active_board"
    q = f"SELECT * FROM {table} WHERE {_LIVE_FILTER} AND {_tier_gate}"
    p = []
    if sport:
        q += " AND sport=?"; p.append(sport)
    if tier:
        q += " AND tier=?"; p.append(tier)
    if stat_filter:
        q += " AND LOWER(stat_type) LIKE ?"; p.append(f"%{stat_filter}%")
    fetch = limit * 3 if limit else -1
    q += " ORDER BY vortex_score DESC"
    if limit:
        q += " LIMIT ?"
        p.append(fetch)
    rows = conn.execute(q, p).fetchall()
    conn.close()
    filtered = []
    for r in rows:
        d = dict(r)
        if d.get("sport") == "MLB":
            st = (d.get("stat_type") or "").lower()
            if not any(kw in st for kw in _ALLOWED_MLB):
                continue
        filtered.append(d)
    return filtered[:limit] if limit else filtered


# ── App ─────────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("WEB_SECRET_KEY") or secrets.token_hex(32)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

# CORS — allow Netlify frontend to call this API
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory=ROOT / "website" / "static"), name="static")


# ── Auth Middleware ─────────────────────────────────────────────────────────

# In-memory state store as session fallback (state -> data)
_state_store: dict[str, dict] = {}


def _store_state(state: str, data: dict):
    _state_store[state] = data


def _pop_state(state: str) -> Optional[dict]:
    return _state_store.pop(state, None)


def _get_user(request: Request) -> Optional[dict]:
    user = request.session.get("user")
    member = request.session.get("guild_member")
    if user and member and oauth.has_access(member):
        return user
    return None


def _require_auth(request: Request):
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized — login with Discord first")
    return user


async def _refresh_guild_member(request: Request):
    """Refresh guild membership data using stored access token."""
    token = request.session.get("access_token")
    user_id = request.session.get("user", {}).get("id")
    if token and user_id:
        member = await oauth.get_guild_member(token, user_id)
        if member:
            request.session["guild_member"] = member
            return member
    return None


# ── Auth Routes ─────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def login(request: Request):
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    _store_state(state, {"state": state})  # fallback
    url = await oauth.get_oauth_url(state)
    return RedirectResponse(url)


@app.get("/auth/callback")
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"""
            <h2>Discord login failed: {error}</h2>
            <p>Try again or contact support.</p>
            <a href="/">Go back</a>
        """, status_code=400)

    # Verify state — try session first, then in-memory store
    log = logging.getLogger("vortex.auth")
    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        stored = _pop_state(state)
        saved_state = (stored or {}).get("state")
    log.info("OAuth callback: code=%s state=%s saved_state=%s",
             code[:10] if code else None, state, saved_state)
    if not saved_state or saved_state != state:
        return HTMLResponse(f"""
            <h2>State mismatch</h2>
            <p>Session cookie may not be persisting. This often happens when:</p>
            <ul>
                <li>Running on <b>localhost</b> — some browsers treat localhost cookies differently</li>
                <li>The server restarted between login and callback</li>
                <li>The redirect URI in Discord Developer Portal doesn't match</li>
            </ul>
            <p>Make sure your redirect URI is exactly: <code>http://localhost:8000/auth/callback</code></p>
            <p><a href="/auth/login">Try again</a></p>
        """, status_code=400)

    if not code:
        return HTMLResponse("<h2>No auth code received</h2>", status_code=400)

    # Exchange code for tokens
    token_data = await oauth.exchange_code(code)
    if not token_data:
        # Check if CLIENT_SECRET is set
        if not os.getenv("DISCORD_CLIENT_SECRET"):
            return HTMLResponse("""
                <h2>❌ Missing Discord Client Secret</h2>
                <p><b>DISCORD_CLIENT_SECRET</b> is not set in your <code>.env</code> file.</p>
                <ol>
                    <li>Go to <a href="https://discord.com/developers/applications" target="_blank">Discord Developer Portal</a></li>
                    <li>Select your application (CLIENT_ID: <code>""" + str(oauth.CLIENT_ID) + """</code>)</li>
                    <li>Go to <b>OAuth2 → General</b></li>
                    <li>Copy the <b>Client Secret</b></li>
                    <li>Add it to <code>.env</code> as <code>DISCORD_CLIENT_SECRET=xxx</code></li>
                    <li>Also add <code>http://localhost:8000/auth/callback</code> to <b>Redirects</b></li>
                    <li>Restart the server</li>
                </ol>
                <a href="/">Go back</a>
            """, status_code=400)
        return HTMLResponse("""
            <h2>❌ Failed to exchange code</h2>
            <p>Discord rejected the authorization. This usually means:</p>
            <ul>
                <li>The redirect URI doesn't match what's in the Discord Developer Portal</li>
                <li>The client secret is incorrect</li>
            </ul>
            <p>Check the server logs for details.</p>
            <a href="/">Try again</a>
        """, status_code=400)

    access_token = token_data.get("access_token")

    # Get user identity
    user = await oauth.get_identity(access_token)
    request.session["user"] = user
    request.session["access_token"] = access_token

    # Get guild membership
    member = await oauth.get_guild_member(access_token, user["id"])
    request.session["guild_member"] = member
    logging.getLogger("vortex.auth").info(
        "Member data: %s has_access=%s", member, oauth.has_access(member) if member else False
    )

    if not member or not oauth.has_access(member):
        return HTMLResponse(
            "<h2>⛔ Access Denied</h2>"
            "<p>You need the <b>Premium</b> or <b>Tester</b> role in the VORTEX Discord server.</p>"
            "<p><a href='/auth/logout'>Log out</a></p>",
            status_code=403,
        )

    return RedirectResponse("/")


@app.get("/auth/me")
async def me(request: Request):
    user = _get_user(request)
    member = request.session.get("guild_member")
    if user and member:
        roles = member.get("roles", [])
        return {
            "username": user.get("username", "?"),
            "avatar": user.get("avatar"),
            "id": user.get("id"),
            "has_premium": oauth.PREMIUM_ROLE_ID in roles,
            "has_tester": oauth.TESTER_ROLE_ID in roles,
        }
    return {"username": None}


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return HTMLResponse("<h2>Logged out</h2><a href='/'>Go home</a>")


# ── API Routes ──────────────────────────────────────────────────────────────

@app.get("/api/pick")
async def api_pick_detail(request: Request, player: str = "", stat: str = ""):
    """Full analysis card for a specific pick — decoded from stats_json."""
    user = _require_auth(request)
    conn = _db()
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    table = "props_board" if "props_board" in tables else "active_board"
    row = conn.execute(
        f"SELECT * FROM {table} WHERE player_name=? AND stat_type=?",
        (player, stat),
    ).fetchone()
    conn.close()
    if not row:
        return {"error": "Pick not found"}
    d = dict(row)
    sj = json.loads(d.get("stats_json") or "{}")
    splits = sj.get("splits", {})
    pitcher = sj.get("pitcher", {})
    bvp = sj.get("bvp", {})
    l5 = splits.get("l5", {})
    l10 = splits.get("l10", {})
    l20 = splits.get("l20", {})
    last_values = splits.get("last_values", [])
    season_sum = sj.get("season_summary", {})
    home_away = sj.get("home_away", {})
    statcast = sj.get("statcast", {})
    game_info = sj.get("game_info", {})
    pitcher_name = sj.get("pitcher_name", pitcher.get("name", ""))
    player_id = sj.get("player_id", d.get("player_id", ""))
    bullpen = sj.get("bullpen", {})
    weather_obj = sj.get("weather", {})
    power_shape = sj.get("power_shape", {})
    team_hitting = sj.get("team_hitting", {})
    return {
        "player":       d.get("player_name", ""),
        "player_id":    player_id,
        "prop":         d.get("stat_type", ""),
        "prop_label":   splits.get("prop_label", d.get("stat_type", "")),
        "line":         d.get("line", ""),
        "tier":         d.get("tier", ""),
        "score":        d.get("vortex_score", 0),
        "ev":           d.get("ev_percentage", 0),
        "side":         sj.get("side", ""),
        "signal":       sj.get("signal_type", ""),
        "trend":        sj.get("trend_signal", ""),
        "platoon":      sj.get("platoon_note", ""),
        "weather_note": sj.get("weather_note", ""),
        "weather":      weather_obj,
        "park":         sj.get("park"),
        "crush":        sj.get("crush_note", ""),
        "defense":      sj.get("defense_note", ""),
        "case":         d.get("case_summary", ""),
        "risk":         d.get("risk_summary", ""),
        "sportsbook":   d.get("sportsbook", ""),
        "season_avg":   splits.get("season_avg"),
        "games_played": splits.get("games_played"),
        "l5":           l5,
        "l10":          l10,
        "l20":          l20,
        "last_values":  last_values,
        "pitcher":      pitcher,
        "pitcher_name": pitcher_name,
        "bvp":          bvp,
        "season_summary": season_sum,
        "home_away":    home_away,
        "statcast":     statcast,
        "game_info":    game_info,
        "bullpen":      bullpen,
        "power_shape":  power_shape,
        "team_hitting": team_hitting,
        "lineup_pos":   sj.get("lineup_pos"),
        "is_home":      sj.get("is_home"),
        "compound_spot": sj.get("compound_spot", False),
        "weather_boost": sj.get("weather_boost", 0),
        "proj_edge":    sj.get("proj_edge"),
        "damage_score": sj.get("damage_score"),
        "stability_tier": sj.get("stability_tier", ""),
        "eff_l10":      sj.get("eff_l10"),
        "eff_l5":       sj.get("eff_l5"),
        "eff_l20":      sj.get("eff_l20"),
        "true_prob":    sj.get("true_prob"),
        "best_odds":    sj.get("best_odds"),
        "ump_name":     sj.get("ump_name", ""),
        "ump_tier":     sj.get("ump_tier", ""),
        "proj_ks":      sj.get("proj_ks"),
        "opp_k":        sj.get("opp_k", {}),
        "opp_kpct":     sj.get("opp_kpct"),
        "last_5_starts": sj.get("last_5_starts", []),
        "recent_k9":    sj.get("recent_k9"),
        "season_stats": sj.get("season_stats", {}),
        "home_era":     sj.get("home_era"),
        "away_era":     sj.get("away_era"),
        "is_pitcher":   sj.get("is_pitcher", False),
        "score_breakdown": sj.get("score_breakdown", {}),
        "opp_stats":    sj.get("opp_stats", {}),
    }


@app.get("/api/picks")
async def api_picks(request: Request, limit: int = 40):
    user = _require_auth(request)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_board, None, None, None, limit if limit != 0 else None)
    if not rows:
        return {"picks": []}
    return {
        "picks": [
            {
                "player":    r.get("player_name", "?"),
                "prop":      r.get("stat_type", "?"),
                "line":      r.get("line", "?"),
                "tier":      r.get("tier", "?"),
                "side":      (json.loads(r.get("stats_json") or "{}").get("side", "")).upper()[:1] or "O",
                "score":     r.get("vortex_score", 0),
                "ev":        r.get("ev_percentage", 0),
                "sport":     r.get("sport", "MLB"),
                "books":     r.get("sportsbook", ""),
                "stats_json": json.loads(r.get("stats_json") or "{}"),
            }
            for r in rows
        ]
    }


@app.get("/api/elite")
async def api_elite(request: Request):
    user = _require_auth(request)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_board, None, None, None, None)
    elite = [r for r in (rows or []) if r.get("tier") == "ELITE"]
    return {
        "elite": [
            {
                "player":    r.get("player_name", "?"),
                "prop":      r.get("stat_type", "?"),
                "line":      r.get("line", "?"),
                "tier":      r.get("tier", "?"),
                "side":      (json.loads(r.get("stats_json") or "{}").get("side", "")).upper()[:1] or "O",
                "score":     r.get("vortex_score", 0),
                "ev":        r.get("ev_percentage", 0),
                "sport":     r.get("sport", "MLB"),
                "stats_json": json.loads(r.get("stats_json") or "{}"),
            }
            for r in elite
        ]
    }


@app.get("/api/nrfi")
async def api_nrfi(request: Request):
    user = _require_auth(request)
    loop = asyncio.get_event_loop()
    plays = await loop.run_in_executor(None, nrfi_module.get_nrfi_plays)
    return {
        "plays": [
            {
                "recommendation": p.get("recommendation"),
                "confidence":     p.get("confidence"),
                "nrfi_score":     p.get("nrfi_score", 0),
                "yrfi_score":     p.get("yrfi_score", 0),
                "home_abbr":      p.get("home_abbr", ""),
                "away_abbr":      p.get("away_abbr", ""),
                "home_pitcher":   p.get("home_pitcher", ""),
                "away_pitcher":   p.get("away_pitcher", ""),
                "factors":        p.get("nrfi_factors", []) or p.get("yrfi_factors", []),
                "game_utc":       p.get("game_utc", ""),
            }
            for p in plays
        ]
    }


@app.get("/api/player")
async def api_player(request: Request, name: str):
    user = _require_auth(request)
    loop = asyncio.get_event_loop()

    player_id = stats_mlb.get_player_id(name)
    if not player_id:
        return {"error": f"Player '{name}' not found"}

    card = await loop.run_in_executor(None, get_research_card, player_id)
    if not card:
        return {"error": "No research data available"}

    card["player_id"] = player_id
    return card


@app.get("/api/board")
async def api_board(request: Request):
    """Full board stats — counts by tier."""
    user = _require_auth(request)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_board, None, None, None, None)
    rows = rows or []
    tiers = {"ELITE": 0, "STRONG": 0, "GOOD": 0, "LEAN": 0}
    for r in rows:
        t = r.get("tier", "LEAN")
        tiers[t] = tiers.get(t, 0) + 1
    return {"total": len(rows), "tiers": tiers}


@app.get("/api/predict")
async def api_predict(request: Request, player: str = "", stat: str = "", line: float = 0, side: str = "over"):
    """Run full analysis pipeline for a manually entered prop — mirrors /prediction."""
    import asyncio as _asyncio
    import analyze as vortex_analyze
    from research import fuzzy_search

    user = _require_auth(request)
    loop = asyncio.get_event_loop()

    stat_key = stat.strip().lower().replace(" ", "_")
    _STAT_ALIASES = {
        "k": "strikeouts", "ks": "strikeouts", "strikeout": "strikeouts", "strikeouts": "strikeouts",
        "h": "hits", "hit": "hits", "hits": "hits",
        "hrr": "hits_runs_rbis", "h+r+rbi": "hits_runs_rbis", "hits+runs+rbis": "hits_runs_rbis",
        "hr": "home_runs", "home_run": "home_runs", "home_runs": "home_runs",
        "tb": "total_bases", "total_bases": "total_bases", "totalbases": "total_bases",
        "rbi": "rbis", "rbis": "rbis",
        "r": "runs_scored", "run": "runs_scored", "runs": "runs_scored", "runs_scored": "runs_scored",
        "bb": "walks", "walk": "walks", "walks": "walks",
        "outs": "pitcher_outs", "po": "pitcher_outs", "pitcher_outs": "pitcher_outs",
        "ha": "pitcher_hits_allowed", "hits_allowed": "pitcher_hits_allowed", "pitcher_hits_allowed": "pitcher_hits_allowed",
        "er": "pitcher_earned_runs", "era": "pitcher_earned_runs", "earned_runs": "pitcher_earned_runs",
        "pitcher_earned_runs": "pitcher_earned_runs",
        "fp": "fantasy_score", "fs": "fantasy_score", "fantasy_score": "fantasy_score",
        "fantasy": "fantasy_score", "fantasy score": "fantasy_score",
    }
    prop_type = _STAT_ALIASES.get(stat_key)
    if not prop_type:
        return {"error": f'Unknown stat "{stat}". Valid: K, H, HRR, HR, TB, RBI, R, BB, FP, HA, ER, PO'}

    if not player.strip():
        return {"error": "Player name is required"}
    if line <= 0:
        return {"error": "Line must be greater than 0"}
    if side not in ("over", "under"):
        return {"error": "Side must be 'over' or 'under'"}

    # Step 1: Resolve player
    try:
        matches = await loop.run_in_executor(None, fuzzy_search, player.strip())
    except Exception as exc:
        return {"error": f"Player lookup failed: {exc}"}
    if not matches:
        return {"error": f'No MLB player found matching "{player}"'}

    found = matches[0]
    player_id = found["id"]
    player_name = found["name"]

    # Resolve team
    _team_map = {
        133:"OAK",134:"PIT",135:"SD",136:"SEA",137:"SF",138:"STL",
        139:"TB",140:"TEX",141:"TOR",142:"MIN",143:"PHI",144:"ATL",
        145:"CWS",146:"MIA",147:"NYM",158:"MIL",108:"LAA",109:"ARI",
        110:"BAL",111:"BOS",112:"CHC",113:"CIN",114:"CLE",115:"COL",
        116:"DET",117:"HOU",118:"KC",119:"LAD",120:"WSH",121:"NYY",
    }
    try:
        _team_id = await loop.run_in_executor(None, lambda: stats_mlb.get_player_current_team(player_id))
        team = _team_map.get(_team_id, found.get("team", ""))
    except Exception:
        team = found.get("team", "")

    # Step 2: Hit rates
    if prop_type == "strikeouts":
        splits = {}
    else:
        try:
            splits = await loop.run_in_executor(None, lambda: vortex_analyze.compute_hit_rates(player_id, line, prop_type))
        except Exception as exc:
            return {"error": f"Stats fetch failed: {exc}"}
        if "error" in splits:
            return {"error": splits["error"]}

    # Step 3: Matchup
    try:
        matchup = await loop.run_in_executor(None, lambda: vortex_analyze.get_matchup_info(player_id))
    except Exception:
        matchup = {}

    pitcher_nm = matchup.get("pitcher")
    pitcher_id = matchup.get("pitcher_id")
    opp_team_id = matchup.get("opp_team_id")

    # Step 4: Parallel enrichment
    async def _safe(fn, default=None):
        try:
            return await loop.run_in_executor(None, fn)
        except Exception:
            return default if default is not None else {}

    (bvp, pitcher, weather, team_bvp_data, oaa_data, arsenal, bat_vs_pitch,
     statcast_data, bullpen_data, umpire_data, batter_hand, lineup_spot,
     team_h2h_data, vs_hand_splits_data) = await _asyncio.gather(
        _safe(lambda: stats_mlb.get_bvp_history(player_id, pitcher_id) if pitcher_id else {}),
        _safe(lambda: stats_mlb.get_pitcher_metrics(pitcher_nm) if pitcher_nm else {}),
        _safe(lambda: stats_mlb.get_game_weather(_team_map.get(matchup.get("home_team_id"), ""), matchup.get("game_utc", "")) if matchup.get("home_team_id") else {}),
        _safe(lambda: stats_mlb.get_team_bvp(player_id, opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_team_defense_oaa(opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_pitcher_arsenal(pitcher_id) if pitcher_id else [], default=[]),
        _safe(lambda: stats_mlb.get_batter_vs_pitch_type(player_id, pitcher_id) if player_id and pitcher_id else [], default=[]),
        _safe(lambda: stats_mlb.get_statcast_by_id(player_id) if player_id else {}),
        _safe(lambda: stats_mlb.get_bullpen_stats(opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_game_umpire(matchup.get("home_team_id")) if matchup.get("home_team_id") else {}),
        _safe(lambda: stats_mlb.get_player_bat_side(player_id) if player_id else "", default=""),
        _safe(lambda: stats_mlb.get_lineup_position(player_id) if player_id else None, default=None),
        _safe(lambda: stats_mlb.get_vs_team_splits(player_id, opp_team_id, line, prop_type) if player_id and opp_team_id else {}),
        _safe(lambda: stats_mlb.get_batter_hand_splits(player_id) if player_id else {}, default={}),
    )

    # Step 5: Opponent K rate
    opp_k_rank = None
    opp_k_pct = None
    try:
        opp_team_name = matchup.get("opponent", "")
        if opp_team_name:
            all_k_rates = stats_mlb.get_all_teams_k_rate()
            for _tid, kd in all_k_rates.items():
                if kd.get("name", "").lower() in opp_team_name.lower() or \
                   opp_team_name.lower() in kd.get("name", "").lower():
                    opp_k_rank = kd.get("rank")
                    _raw = kd.get("k_pct")
                    opp_k_pct = (_raw / 100) if _raw is not None else None
                    break
    except Exception:
        pass

    # Park factor
    park_factor = 1.0
    try:
        opp_name = matchup.get("opponent", "")
        is_home = matchup.get("is_home")
        if is_home is False and opp_name:
            park_factor = stats_mlb.PARK_FACTOR.get(opp_name, 1.0)
        elif is_home is True:
            for full_name, pf in stats_mlb.PARK_FACTOR.items():
                if team and team.upper() in full_name.upper():
                    park_factor = pf
                    break
    except Exception:
        pass

    # K-prop override
    if prop_type == "strikeouts":
        try:
            _k_card = await loop.run_in_executor(
                None, lambda: stats_mlb.get_pitcher_k_card(player_name, line, opp_team_id, pitcher_id=player_id)
            )
            if _k_card.get("error"):
                return {"error": f'No K data for {player_name}: {_k_card["error"]}'}
            _ks = dict(_k_card.get("splits", {}))
            _ks["recent_games"] = [
                {"date": s.get("date",""), "opponent": s.get("opponent",""), "value": s.get("k",0), "over": s.get("k",0) > line}
                for s in _k_card.get("last_5_starts", [])
            ]
            splits = _ks
            pitcher = _k_card
            opp_k_d = _k_card.get("opp_k") or {}
            if opp_k_d:
                opp_k_rank = opp_k_d.get("rank")
                _raw_kpct = opp_k_d.get("k_pct")
                opp_k_pct = (_raw_kpct / 100) if _raw_kpct is not None else None
        except Exception as exc:
            return {"error": f"K-prop lookup failed: {exc}"}

    # Step 6: Grade
    try:
        grade = vortex_analyze.grade_pick(
            splits, line, side=side,
            opp_k_rank=opp_k_rank, opp_k_pct=opp_k_pct,
            pitcher=pitcher, bvp=bvp, park_factor=park_factor,
            weather=weather, team_bvp=team_bvp_data, oaa=oaa_data,
            prop_type=prop_type, lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
            statcast=statcast_data or None, team_h2h=team_h2h_data or None,
            arsenal=arsenal or None, bat_vs_pitch=bat_vs_pitch or None,
            vs_hand_splits=vs_hand_splits_data or None,
        )
    except Exception as exc:
        return {"error": f"Grading failed: {exc}"}

    # Step 7: Build embed via bot's own builder, extract fields as JSON
    try:
        embed = vortex_analyze.build_analyze_embed(
            player_name=player_name,
            team=team,
            prop_type=prop_type,
            line=line,
            splits=splits,
            grade=grade,
            matchup=matchup,
            bvp=bvp,
            side=side,
            pitcher_card=pitcher if pitcher and not pitcher.get("error") else None,
            weather=weather,
            team_bvp=team_bvp_data,
            oaa=oaa_data,
            arsenal=arsenal,
            bat_vs_pitch=bat_vs_pitch,
            statcast=statcast_data,
            bullpen=bullpen_data,
            umpire=umpire_data,
            batter_hand=batter_hand or "",
            park_factor=park_factor,
            lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
            vs_hand_splits=vs_hand_splits_data or None,
            team_h2h=team_h2h_data or None,
        )
        fields = [{"name": f.name, "value": f.value} for f in embed.fields]
        title = embed.title or ""
        description = embed.description or ""
        footer_text = embed.footer.text if embed.footer else ""
    except Exception as exc:
        import traceback
        traceback.print_exc()
        fields = []
        title = ""
        description = ""
        footer_text = ""

    return {
        "player": player_name,
        "player_id": player_id,
        "team": team,
        "prop": stat,
        "prop_type": prop_type,
        "line": line,
        "side": side,
        "score": grade.get("score", 0),
        "tier": grade.get("label", ""),
        "embed_title": title,
        "embed_description": description,
        "embed_fields": fields,
        "embed_footer": footer_text,
        "splits": splits,
        "pitcher": pitcher if pitcher and not pitcher.get("error") else {},
        "bvp": bvp,
        "matchup": matchup,
        "weather": weather,
        "park_factor": park_factor,
        "statcast": statcast_data,
        "bullpen": bullpen_data,
        "umpire": umpire_data,
        "arsenal": arsenal,
        "lineup_spot": lineup_spot,
        "batter_hand": batter_hand,
        "vs_hand_splits": vs_hand_splits_data,
        "opp_k_rank": opp_k_rank,
        "opp_k_pct": opp_k_pct,
    }


@app.get("/api/record")
async def api_record(request: Request, tier: str = "all"):
    """Simple record endpoint."""
    user = _require_auth(request)
    conn = _db()
    rows = conn.execute(
        "SELECT tier, result, COUNT(*) as total FROM predictions GROUP BY tier, result"
    ).fetchall()
    conn.close()
    return {"rows": [dict(r) for r in rows]}


# ── Frontend ────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    with open(ROOT / "website" / "static" / "index.html", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run("website.main:app", host="0.0.0.0", port=port, reload=True)
