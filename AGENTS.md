# VORTEX — Session Log

## Goal
Build and deploy VORTEX — a Discord bot that grades MLB player props using free stats APIs and returns confidence scores with tiered play recommendations.

## Constraints & Preferences
- Must use free/direct MLB Stats API with no API key.
- Cloudflare Worker proxy masks server IP to avoid blocks; fallback to direct `statsapi.mlb.com` if proxy fails.
- Only ELITE and STRONG tier plays appear in `/picks`, `/record`, and the `predictions` table.
- `/picks` shows all ELITE/STRONG plays uncapped; dropdown limited to first 25 (Discord limitation).
- Hit rate is the primary signal; EV is secondary.
- `/record` is admin-only with a dropdown tier filter.
- Coin-flip filter drops 48-52% L10 hit rates; scratch detection skips scratched batters; compound spot flags vulnerable starter+bullpen combos; power shape labels from Statcast barrel/hard-hit data; `/slate` ranks starting pitchers by matchup difficulty.
- PrizePicks fantasy score uses a fixed formula (singles×3, doubles×5, triples×8, HR×10, runs×2, RBI×2, BB×2, HBP×2, SB×5).
- Analysis card and research dropdown results are **public** (non-ephemeral); board embeds from `/picks`, `/elite`, `/top` etc. stay **ephemeral**; error messages stay ephemeral.
- All dates must use `vortextime.vortex_day()` — never `_date.today()` — to avoid timezone drift.

## Progress

### Done
- **Proxy URL fixed** — `stats_mlb.py:BASE` uses `-d45` Cloudflare Worker endpoint.
- **Added `/sports/1/players` fallback** in `get_player_id()` — fuzzy match when `/people/search` fails.
- **Retrofitted all modules** to use `stats_mlb.BASE` (proxy): `research.py`, `grade_results.py`, `stats_mlb_enrichment.py`.
- **Increased `MAX_BOARD`** 30 → 40 in `update_board.py`.
- **Removed loading messages** from `/player` and modal commands in `vortex.py`.
- **Tier override** — `update_board.py` uses `grade_pick` label (not `_confidence_tier`) as single source of truth for board tier.
- **`_log_predictions` filtered** — only ELITE/STRONG rows inserted into `predictions` table.
- **`get_board()` supports `limit=None`** — `/picks` returns all ELITE/STRONG uncapped.
- **Embed note >25 props** — tells users to use `/prediction <player>` for props beyond the dropdown.
- **Handedness text fixed** in `analyze.py` — outcome descriptions side-aware.
- **Strikeout matchup scoring fixed** in `analyze.py` — ±3/±5 per side with severity thresholds.
- **`/record` tier filter dropdown** in `vortex.py` — `RecordFilterSelect` with All/Elite/Strong/Good/Elite+Strong/Strong+Good.
- **Cloudflare Worker deployed** at `mlb-proxy.damian209466-d45.workers.dev`.
- **Coin-flip filter** — `update_board.py:_should_include` drops 48-52% L10 hit rates unless tier is ELITE.
- **Scratch detection** — `stats_mlb.get_game_lineup_ids()` + `enrich_mlb` skips scratched batters.
- **Compound spot flag** — `enrich_mlb` sets `compound_spot: bool` in `stats_json` when starter ERA ≥4.5 or HR/9 ≥1.2 AND bullpen tier is WEAK/AVERAGE.
- **`/slate` command** in `vortex.py` — "Attack Board" ranks all starting pitchers by difficulty score.
- **4 pitcher prop types added** — `pitcher_outs`, `pitcher_hits_allowed`, `pitcher_earned_runs` in `update_board.py`, `vortex.py`, `analyze.py`, `grade_results.py`.
- **Odds read in `/analyze`** — `ParlayLegSelect.callback` passes `ev_pct`, `book_name`, `book_odds` to `build_analyze_embed`.
- **Weak parlay leg identification** — `_parlay_legs` computes `leg_quality` (Anchor/Core/Conflict/Fill/Filler) in parlay embed.
- **Power shape label** — `enrich_mlb` adds `power_shape` dict (barrel%, hard-hit%, label) to `stats_json`.
- **`batter_fantasy_score` (PrizePicks) market added** — full computation engine across all 5 modules.
- **`HA`, `PO`, `ERA` stat shortcuts added** — `"ha": "pitcher_hits_allowed"`, `"po": "pitcher_outs"`, `"era": "pitcher_earned_runs"` in `_STAT_ALIASES`. Error message updated with all 12 valid shortcuts.
- **OCR patterns added/reordered** in `analyze.py` — `(?<!\w)po(?!\w)`, `(?<!\w)ha(?!\w)`, `(?<!\w)era(?!\w)`, `earned\s+runs?\s*allowed` patterns. Pitcher-specific patterns moved before generic single-word patterns.
- **`get_historical_splits` pitcher support** in `stats_mlb.py` — uses `group: "pitching"` for pitcher props.
- **Grading pipeline expanded** in `grade_results.py` — boxscore extraction reads `pit.outs`, `pit.hits`, `pit.earnedRuns`.
- **`/slate` team abbreviation fix** — keys changed to `home_team_name`/`away_team_name`, display uses `home_abbr`/`away_abbr`.
- **`/slate` bullpen tier fix** — `get_bullpen_stats` didn't return a `tier` field; now computed inline from ERA: ≤3.50 ELITE, ≤4.20 SOLID, ≤5.00 AVERAGE, else WEAK.
- **7-component pitching prop scoring engine** — `_enrich_pitcher_stat_row` rewritten in `update_board.py`.
- **`get_team_opponent_stats(team_id)`** added to `stats_mlb.py`.
- **`get_pitcher_advanced_stats(pitcher_id)`** added to `stats_mlb.py`.
- **`get_pitcher_metrics` game log expanded** — `last_5_starts` entries now include `hits` and `outs` fields.
- **`_PITCHER_PROP_CONFIG["pitcher_outs"]["recent_key"]`** changed from `ip` to `outs`.
- **`requirements.txt` updated** — added `cloudscraper>=1.2.71` and `curl_cffi>=0.7.4`.
- **`mlb-proxy/src/index.js` updated** — added `/prizepicks/*` routing.
- **`/goblins` reverted to board data** — PrizePicks API blocked by PerimeterX.
- **`/cleanup` command** — admin-only, deletes non-ELITE/STRONG predictions.
- **Handedness 🟢/🔴 icon fix** in `analyze.py` — icons now flip based on play direction.
- **OCR debug text removed** from `/analyze`.
- **"Hitter FS" / "FS" OCR pattern** added to `analyze.py`.
- **`fs` and `hitter fs` stat aliases** added to `_STAT_ALIASES`.
- **`start.sh` auto-restart wrapper** created.
- **TypeError crash fix** in `update_board.py` — `l10_raw` was `None` when pitcher splits had `{"rate": None}`. Fixed by `.get("rate") or 50`.
- **`/refresh` success message fix** — changed `/menu` to `/dashboard` in `vortex.py` line 3047.
- **Ephemeral messages removed from analysis cards** — `/analyze` defer + multi-prop message, `PropSelect.callback`, `StatSelect.callback`, `MultiPropSelect.callback`, `_run_analyze` final embed, `PlayerLookupModal` all changed to non-ephemeral. Board embeds (BoardView buttons, `/picks`, `/elite`, etc.) and error messages stay ephemeral.
- **`/maintenance` command added** — admin-only toggle. When ON, blocks ALL non-admin commands with a "🔧 VORTEX — MAINTENANCE MODE" embed.
- **Per-dimension learned weight modifiers (Option 2)** — fully implemented.
- **Date fix in analysis cards** — `analyze.py` footer now uses `vortextime.vortex_day()`.
- **TBD pitcher display** — `analyze.py` line 1186 changed `or ""` to `or "TBD"`.
- **Confirmed pitcher override from lineups endpoint** — `_get_confirmed_pitchers()` in `stats_mlb.py` fetches confirmed starters via `hydrate: "lineups"`. Uses correct `homePlayers`/`awayPlayers` API keys with position filter for pitcher. `confirmed_pitchers_` in `_VOLATILE_PREFIXES` (14h TTL).
- **Board auto-advances date after 8 PM Mountain** — new `vortex_board_day()` in `vortextime.py` checks UTC-10 frame hour ≥20, auto-advances to tomorrow. Verified with 9-game board for June 18 instead of 15-game June 17.
- **Odds API 401 fixed** — separate `ODDS_SESSION` without MLB.com spoofed headers created. All 4 Odds API calls use `ODDS_SESSION.get()`.
- **Analysis card wrong pitcher fix** — `get_matchup_info()` in `analyze.py` now tries `vortex_board_day()` first, then `vortex_day()`. No more `game_date` threading through callbacks.
- **`sqlite3.Row` `.get()` crash fixed** — all three dropdown callbacks (`PropSelect`, `StatSelect`, `ParlayLegSelect`) changed from `.get()` to bracket access with `try/except` or `in r.keys()` guard.
- **`get_todays_game_times()` uses `vortex_day()`** in `stats_mlb.py` instead of `_date.today()` for timezone consistency.
- **NRFI/YRFI feature (Jun 18)** — `backend/nrfi.py` fully rewritten. Lineup gate requires top-3 batters per side (lineup hydrate, ~1h pre-game). 14-factor scoring: K/9, BB/9, barrel%, hard-hit%, ERA, WHIP, opponent RPG, park factor, pitcher hot/cold, 1st-inning splits, top-3 batter OPS, K%, platoon edge. Data from MLB Stats proxy + Baseball Savant CSVs. Pitchers from probablePitcher (lineups hydrate doesn't contain pitchers). `gamePk` added to `get_todays_schedule()` output. `/nrfi` command posts to #nrfi channel, responds ephemeral.

### In Progress
- **Website (FastAPI + animated vortex frontend)** — `website/` directory complete. Discord OAuth with role-based access (Premium/Tester). Needs `DISCORD_CLIENT_SECRET` in `.env` before deployment.

### Blocked
- **PrizePicks API unreachable** — PerimeterX CAPTCHA protection blocks all programmatic access. `/goblins` reverted to board data as workaround.
- **Bot stability** — bot randomly goes down with "Connection error: Failed to connect to server console" on Wispbyte.

## Key Decisions
- **`grade_pick` as single source of truth** for scoring and tier.
- **Strikeout matchup adjustments ±3/±5** — aggressive enough to influence score without overriding primary L10/L20 hit-rate signal.
- **`predictions` table only stores ELITE/STRONG** — lower-tier plays removed to keep reported accuracy clean.
- **Proxy over direct API for all production code** — every module routes through Cloudflare Worker.
- **Coin-flip filter drops 48-52%** — near-50% hit rates are noise; ELITE tier overrides.
- **Pitcher props graded via game-log hit rates** — same tier logic as batter props.
- **Pitcher props share board cap with strikeouts** — `MAX_PITCHER_K=5` reservation applies to all pitcher markets.
- **7-component scoring engine** replaces simple L10 scoring for pitcher stat props.
- **PrizePicks PerimeterX blocking is permanent** — no HTTP-level bypass possible.
- **Handedness icon is direction-aware** — 🟢/🔴 must consider Over vs Under.
- **`None`-safe rate defaults** — `.get("rate", 50)` fails when key exists with `None`; use `.get("rate") or 50`.
- **Maintenance mode blocks everyone including beta role** — only admin role bypasses.
- **Analysis cards public, board embeds ephemeral** — users agreed.
- **Lineups hydrate excludes pitchers** — only the 9 hitters appear in `homePlayers`/`awayPlayers`. Pitchers come from `probablePitcher` data. Confirmed pitcher override from lineups is a dead end.
- **Board date auto-advances at 8 PM Mountain** — `vortex_board_day()` advances to tomorrow when UTC-10 frame hour ≥20, preventing re-hashing finished games.
- **Separate session for Odds API** — `ODDS_SESSION` has no MLB.com spoofed headers; `SESSION` keeps them for MLB Stats API.
- **Dual-date fallback for analysis card pitchers** — `get_matchup_info()` tries `vortex_board_day()` first, falls back to `vortex_day()`. Simpler and more reliable than threading `game_date` through callbacks.
- **sqlite3.Row `.get()` not supported** — use bracket access `row["col"]` with `try/except` guard instead.
- **NRFI/YRFI Statcast data: only non-None factors used** — barrel/hard-hit data from Baseball Savant `/leaderboard/statcast` (pitcher type) with `brl_percent` and `ev95percent` columns. Pitchers without Statcast data don't get barrel/hard-hit factors. xERA/xwOBA from `/leaderboard/expected_statistics` (pitcher type). Factors deduplicated before embed display.
- **Moneyline model v3** — added offensive quality (wRC+, ISO, BB%, K%), pitcher venue splits (home/away FIP/ERA), season series H2H records, enhanced bullpen (L7 ERA + fatigue count). 3 new `stats_mlb.py` functions: `get_team_offensive_profile()`, `get_pitcher_venue_splits()`, `get_team_h2h_record()`. 4 new `moneyline.py` nudges: `_offensive_quality_nudge()`, `_pitcher_venue_nudge()`, `_h2h_nudge()`, `_bullpen_enhanced_nudge()`. All factors capped and sample-weighted.

## Next Steps
1. Verify NRFI/YRFI runs correctly on next day's slate (verified Jun 18 — all 7 Pre-Game/Final games produce YRFI plays)
2. Monitor bot stability on Wispbyte
3. Verify learned weights work end-to-end (min 20 graded predictions per dimension)

## Critical Context
- **Proxy URL**: `https://mlb-proxy.damian209466-d45.workers.dev/api/v1`
- **Worker code** in `mlb-proxy/src/index.js`.
- **PrizePicks PerimeterX**: `appId: PXZNeitfzP`, all HTTP-level bypasses failed.
- **`curl_cffi` v0.15.0** installed — `"chrome123"` supported but still gets 403 from PerimeterX.
- **`/slate` bullpen tier**: computed inline from ERA in `/slate` command.
- **Prediction accuracy (post-cleanup)**: Overall 54.4% (137/252 graded). ELITE 67.7% (63/93), STRONG 64.8% (57/88).
- **7-component scoring engine**: score ≥8=ELITE, ≥5=STRONG, ≥3=GOOD, ≥1=LEAN, else PASS.
- **`score_weights` table populated but now read** — `_rebuild_weights` writes hit-rate-derived weights; `_load_learned_weights` reads them at startup.
- **Confirmed pitcher override**: `_get_confirmed_pitchers(date_str)` fetches lineups hydrate; uses `homePlayers`/`awayPlayers` keys with position filter for pitcher.
- **Vortex day rolls at 10:00 UTC (4 AM Mountain)** — `vortex_day()` uses UTC-10 offset. `vortex_board_day()` auto-advances after 8 PM Mountain (UTC-10 hour ≥20).
- **Odds API sessions**: `SESSION` for MLB Stats API (with Origin/Referer headers), `ODDS_SESSION` for Odds API (clean headers).
- **Admin role ID**: `1516353685402292274`. Beta role ID: `1515612947110690846`. Guild: `1515224924267216926`.
- **NRFI channel ID**: `1517263414110453970`.
- **Odds API key**: `50ed5efdcaf1a53d6216ee4df1e45b09` (in `.env` at `Vortex/.env`).
- **Startup command**: `update_board.py` runs first, then `vortex.py`.
- **`sqlite3.Row` objects** don't support `.get()` — use bracket access `row["col"]` with guard.

## Relevant Files
- `backend/prizepicks.py`: PrizePicks API fetcher — **currently unused** due to PerimeterX blocking.
- `backend/stats_mlb.py`: `BASE` fixed to `-d45`; `/sports/1/players` fallback; `get_game_lineup_ids()` for scratch detection; `PROP_STAT_MAP` includes all markets; `get_historical_splits()` pitcher-aware; `get_pitcher_advanced_stats()`; `get_pitcher_metrics()` expanded; `get_team_opponent_stats()`; `_get_confirmed_pitchers()` fixed with `homePlayers`/`awayPlayers` keys + position filter; `get_todays_game_times()` uses `vortex_day()`; `get_team_offensive_profile()` (wRC+, ISO, BB%, K%); `get_pitcher_venue_splits()` (home/away FIP/ERA); `get_team_h2h_record()` (season series).
- `backend/update_board.py`: Board engine v5; `compute_score` 12-factor 0-100; `_enrich_pitcher_stat_row` 7-component engine; `_should_include` with anti-slump/coin-flip/min-line guards; `MAX_BOARD=40`; `_load_learned_weights()`, `_compute_learned_multiplier()`, `_apply_learned_weight()`; `ODDS_SESSION` for Odds API; `SESSION` for MLB Stats API.
- `backend/analyze.py`: `get_matchup_info()` now tries `vortex_board_day()` first, then `vortex_day()` for correct analysis card pitchers.
- `backend/grade_results.py`: `_rebuild_accuracy`; `_rebuild_weights`; `_log_predictions` (ELITE/STRONG only); pitcher boxscore extraction.
- `backend/init_db.py`: `score_weights` table schema.
- `backend/research.py`: `fuzzy_search` for player lookup; `build_research_card`.
- `backend/vortex_analyze.py`: `extract_slip_data()` OCR extraction.
- `backend/vortextime.py`: `vortex_day()`, `vortex_now()`, `vortex_day_offset()`, `vortex_board_day()` (auto-advances at 8 PM Mountain).
- `backend/nrfi.py`: `_load_pitcher_statcast_leaderboard()` fetches 3 Savant CSVs; `_nrfi_subscore()` dual-factor scoring; `get_nrfi_plays()` main entry; `build_nrfi_embed()` Silas-style discord embed.
- `backend/moneyline.py`: Moneyline model v3 — offensive quality (wRC+, ISO, BB%, K%), pitcher venue splits, H2H records, enhanced bullpen (L7 ERA + fatigue count). `_offensive_quality_nudge()`, `_pitcher_venue_nudge()`, `_h2h_nudge()`, `_bullpen_enhanced_nudge()`.
- `bot/vortex.py`: Discord bot; all commands; `/cleanup`; `/record` tier filter; `/refresh`; `/analyze`, `/prediction`, `/player`, `/parlay`, `/goblins`, `/slate`, `/nrfi`; non-ephemeral analysis cards; ephemeral board embeds; sqlite3.Row bracket access fixes.
- `mlb-proxy/src/index.js`: Cloudflare Worker proxy; `/prizepicks/*` routing.
- `start.sh`: Auto-restart wrapper.
- `.env` (at `Vortex/.env`): Contains `DISCORD_TOKEN`, `CLIENT_ID`, `ODDS_API_KEY`, `OCR_API_KEY`, `ANTHROPIC_API_KEY`.
