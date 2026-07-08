"""
VORTEX V2 props board builder.

Cost-control design (this is the whole point of this module): scoring every
batter in today's slate against the trained models costs ZERO Odds API
credits (it's all free MLB Stats API calls). Only AFTER ranking every
candidate do we decide which handful of games are worth spending odds
credits on, and only fetch real lines for that shortlist -- never the whole
day's slate. See the VORTEX V2 plan's "Known limitations" for why the
500-credits/month budget makes a full-slate daily refresh unaffordable.

Second honest limitation, discovered by inspecting real posted odds while
building this: sportsbooks do NOT use one fixed line per stat_type -- a star
hitter might be posted at "2.5 total bases" while a bench player is posted
at "0.5". Every VORTEX V2 model is only trained to judge ONE fixed
STANDARD_LINES threshold per stat_type (v2/common/stat_types.py), so it
cannot correctly score a prop posted at a different number. This builder
therefore only matches a shortlisted candidate to a real market when the
book's posted `point` exactly equals our trained threshold, and skips (does
not silently mis-score) anything else. This shrinks how many real props can
be confirmed per run -- that's expected until a future model version can
take the line itself as an input.

Covers batter props (hits, total bases, RBIs, runs scored, hits+runs+RBIs,
fantasy score -- home runs deliberately excluded, product decision) and
pitcher props (strikeouts, hits allowed, earned runs, outs), scored for
every probable batter and today's starting pitchers.

Run directly:
    python build_board.py --top-per-stat 4 --min-edge 0.05
"""
import json
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import stats_mlb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.odds_math import american_to_prob, devig_two_way  # noqa: E402
from v2.common.stat_types import BATTER_STAT_TYPES, PITCHER_STAT_TYPES, STANDARD_LINES, STAT_LABELS  # noqa: E402
from v2.inference.features import build_live_features, build_live_pitcher_features  # noqa: E402
from v2.inference.predict import predict_all, predict_all_pitcher  # noqa: E402
from v2.board.odds_client import list_events, fetch_event_props, MARKET_FOR_STAT  # noqa: E402
from v2.board.traps import detect_batter_traps, detect_pitcher_traps  # noqa: E402

ALL_STAT_TYPES = BATTER_STAT_TYPES + PITCHER_STAT_TYPES

OUT_PATH = Path(__file__).resolve().parent / "data" / "board_today.json"
RAW_ODDS_DIR = Path(__file__).resolve().parent / "data" / "raw_odds"


def _norm_team(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").lower().strip()
    return name


def _norm_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    return name.lower().strip()


def score_todays_slate() -> tuple[list, list]:
    """Free pass: every probable batter AND today's starting pitchers,
    scored by the trained models. No Odds API calls happen in this function.

    Returns (candidates, bait): bait is the Bait Props (trap) cards from
    v2/board/traps.py -- still zero Odds API credits, it's all MLB Stats
    API lookups."""
    schedule = stats_mlb.get_todays_schedule()
    batter_jobs = []
    pitcher_jobs = []
    for game_pk, game in schedule.items():
        for side in ("home", "away"):
            opp_side = "away" if side == "home" else "home"
            team_id = game.get(f"{side}_team_id")
            opp_team_id = game.get(f"{opp_side}_team_id")
            opp_pitcher_id = game.get(f"{opp_side}_pitcher_id")
            opp_pitcher_name = game.get(f"{opp_side}_pitcher")
            if team_id:
                lineup = stats_mlb.get_team_lineup(team_id)
                if not lineup:
                    lineup = stats_mlb.get_team_hitters_roster(team_id)
                for batter in lineup:
                    pid = batter.get("id")
                    name = batter.get("name") or batter.get("fullName")
                    if not pid or not name:
                        continue
                    batter_jobs.append({
                        "game_pk": game_pk,
                        "home_team_name": game.get("home_team_name"),
                        "away_team_name": game.get("away_team_name"),
                        "player_id": pid,
                        "player_name": name,
                        "is_home": side == "home",
                        "opp_team_id": opp_team_id,
                        "opp_pitcher_id": opp_pitcher_id,
                        "opp_pitcher_name": opp_pitcher_name,
                    })

            pitcher_id = game.get(f"{side}_pitcher_id")
            pitcher_name = game.get(f"{side}_pitcher")
            if pitcher_id and pitcher_name:
                pitcher_jobs.append({
                    "game_pk": game_pk,
                    "home_team_name": game.get("home_team_name"),
                    "away_team_name": game.get("away_team_name"),
                    "player_id": pitcher_id,
                    "player_name": pitcher_name,
                    "is_home": side == "home",
                    "opp_team_id": opp_team_id,
                })

    # Each job is one live network fetch (a player's current-season gamelog).
    # Sequentially this was ~65s for a single 8-game slate locally -- almost
    # certainly why the first live scan hit Vercel's function timeout.
    # Parallelized the same way prediction_core.py's compute_slate() already
    # does for its own live lookups (ThreadPoolExecutor, 16 workers).
    candidates = []
    trap_jobs = []  # (job, is_pitcher, probs) for the parallel trap pass below
    with ThreadPoolExecutor(max_workers=16) as pool:
        future_to_job = {}
        for job in batter_jobs:
            fut = pool.submit(build_live_features, job["player_id"], job["is_home"])
            future_to_job[fut] = (job, False)
        for job in pitcher_jobs:
            fut = pool.submit(build_live_pitcher_features, job["player_id"], job["is_home"])
            future_to_job[fut] = (job, True)

        for future in as_completed(future_to_job):
            job, is_pitcher = future_to_job[future]
            try:
                feats = future.result()
            except Exception:
                continue
            if feats is None:
                continue
            probs = predict_all_pitcher(feats) if is_pitcher else predict_all(feats)
            for stat_type, prob in probs.items():
                candidates.append({
                    "game_pk": job["game_pk"],
                    "home_team_name": job["home_team_name"],
                    "away_team_name": job["away_team_name"],
                    "player_id": job["player_id"],
                    "player_name": job["player_name"],
                    "stat_type": stat_type,
                    "model_prob": round(prob, 4),
                })
            trap_jobs.append((job, is_pitcher, probs))

    # Bait-prop (trap) detection, parallelized like the feature pass above:
    # a streaky player costs a few extra live matchup lookups (splits/BvP/
    # pitcher metrics), so keep them off the sequential path -- a slate's
    # worth run sequentially could crawl a deployed scan into the function
    # timeout the feature pass already hit once.
    bait = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(detect_pitcher_traps if is_pitcher else detect_batter_traps, job, probs)
            for job, is_pitcher, probs in trap_jobs
        ]
        for future in as_completed(futures):
            try:
                bait.extend(future.result())
            except Exception:
                pass  # a trap-detection failure must never cost the board anything
    return candidates, bait


def shortlist(candidates: list, top_per_stat: int) -> list:
    out = []
    for stat_type in ALL_STAT_TYPES:
        pool = sorted(
            (c for c in candidates if c["stat_type"] == stat_type),
            key=lambda c: c["model_prob"], reverse=True,
        )
        out.extend(pool[:top_per_stat])
    return out


def _match_event(game: dict, events: list) -> dict | None:
    home = _norm_team(game.get("home_team_name"))
    away = _norm_team(game.get("away_team_name"))
    for ev in events:
        ev_home = _norm_team(ev.get("home_team"))
        ev_away = _norm_team(ev.get("away_team"))
        if (home in ev_home or ev_home in home) and (away in ev_away or ev_away in away):
            return ev
    return None


def attach_real_odds(shortlisted: list) -> list:
    """Costs credits -- fetches real props ONLY for the distinct games the
    shortlist actually needs, and only accepts a match at our trained
    STANDARD_LINES threshold (see module docstring)."""
    if not shortlisted:
        return []

    # This disk cache is a local-debugging convenience only (avoids re-spending
    # odds credits across repeated runs while iterating). Deployed Vercel
    # functions have a read-only filesystem outside /tmp, so degrade to
    # "no local cache" there instead of crashing -- the real, durable result
    # of a scan is store.set(BOARD_STORAGE_KEY, ...) in build(), not this file.
    try:
        RAW_ODDS_DIR.mkdir(parents=True, exist_ok=True)
        disk_cache_ok = True
    except OSError:
        disk_cache_ok = False

    events = list_events()  # free
    needed_games = {c["game_pk"]: c for c in shortlisted}
    event_odds_by_game_pk = {}
    for game_pk, sample in needed_games.items():
        raw_cache_file = (RAW_ODDS_DIR / f"{_date.today().isoformat()}_{game_pk}.json") if disk_cache_ok else None
        if raw_cache_file and raw_cache_file.exists():
            print(f"  [cache] using saved odds for game_pk={game_pk} (no credits spent)")
            event_odds_by_game_pk[game_pk] = json.loads(raw_cache_file.read_text(encoding="utf-8"))
            continue
        ev = _match_event(sample, events)
        if not ev:
            print(f"  [warn] no Odds API event match for game_pk={game_pk} "
                  f"({sample.get('away_team_name')} @ {sample.get('home_team_name')})")
            continue
        try:
            data = fetch_event_props(ev["id"])
            event_odds_by_game_pk[game_pk] = data
            if raw_cache_file:
                raw_cache_file.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:
            print(f"  [error] odds fetch failed for game_pk={game_pk}: {exc}")

    results = []
    for cand in shortlisted:
        data = event_odds_by_game_pk.get(cand["game_pk"])
        if not data:
            continue
        market_key = MARKET_FOR_STAT[cand["stat_type"]]
        line = STANDARD_LINES[cand["stat_type"]]
        target_name = _norm_name(cand["player_name"])

        over_prices, under_prices = [], []
        seen_points = set()
        for bm in data.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != market_key:
                    continue
                for outcome in mkt.get("outcomes", []):
                    if _norm_name(outcome.get("description", "")) != target_name:
                        continue
                    seen_points.add(outcome.get("point"))
                    if outcome.get("point") != line:
                        continue  # book posted a different line than our model was trained on -- skip, don't mis-score
                    if outcome.get("name") == "Over":
                        over_prices.append(outcome["price"])
                    elif outcome.get("name") == "Under":
                        under_prices.append(outcome["price"])

        if not over_prices or not under_prices:
            if seen_points:
                print(f"  [skip] {cand['player_name']} {cand['stat_type']}: book has "
                      f"{sorted(seen_points)} posted, model trained on {line} -- no exact match")
            else:
                print(f"  [skip] {cand['player_name']} {cand['stat_type']}: no {market_key} "
                      f"market found for this player in this game's odds at all")
            continue  # no matching market at our trained line for this player

        median_over = sorted(over_prices)[len(over_prices) // 2]
        median_under = sorted(under_prices)[len(under_prices) // 2]
        p_over_raw = american_to_prob(median_over)
        p_under_raw = american_to_prob(median_under)
        p_over_fair, _ = devig_two_way(p_over_raw, p_under_raw)

        cand = dict(cand)
        cand.update({
            "line": line,
            "stat_label": STAT_LABELS.get(cand["stat_type"], cand["stat_type"]),
            "n_books": len(over_prices),
            "market_prob_over": round(p_over_fair, 4),
            "edge": round(cand["model_prob"] - p_over_fair, 4),
        })
        results.append(cand)
    return results


def tier_for_edge(edge: float) -> str:
    if edge >= 0.08:
        return "ELITE"
    if edge >= 0.04:
        return "STRONG"
    return "PASS"


MAX_BAIT_CARDS = 15


def build(top_per_stat: int = 4, min_edge: float = 0.0) -> dict:
    print("Scoring today's slate (free, no odds credits used)...")
    candidates, bait = score_todays_slate()
    bait.sort(key=lambda c: c["severity"], reverse=True)
    bait = bait[:MAX_BAIT_CARDS]
    print(f"  scored {len(candidates)} player/stat_type candidates, {len(bait)} bait props flagged")

    picks = shortlist(candidates, top_per_stat)
    print(f"Shortlisted {len(picks)} candidates across {len(ALL_STAT_TYPES)} stat types "
          f"({len({p['game_pk'] for p in picks})} distinct games) -- fetching real odds for these only...")

    scored = attach_real_odds(picks)
    for p in scored:
        p["tier"] = tier_for_edge(p["edge"])
    scored = [p for p in scored if p["edge"] >= min_edge]
    scored.sort(key=lambda p: p["edge"], reverse=True)

    # Local-debugging convenience only -- deployed Vercel functions have a
    # read-only filesystem outside /tmp. The real, durable result of a scan
    # is api/v2-admin.py's store.set(BOARD_STORAGE_KEY, ...) call, not this
    # file, so skip it silently there instead of crashing the whole scan.
    try:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps({"date": _date.today().isoformat(), "props": scored, "bait": bait},
                                       indent=2),
                             encoding="utf-8")
        print(f"Wrote {len(scored)} props + {len(bait)} bait cards to {OUT_PATH}")
    except OSError:
        pass
    return {"props": scored, "bait": bait}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--top-per-stat", type=int, default=4,
                         help="how many top model-ranked candidates per stat_type to spend odds credits confirming")
    parser.add_argument("--min-edge", type=float, default=0.0,
                         help="drop props below this model-vs-market edge")
    args = parser.parse_args()
    result = build(top_per_stat=args.top_per_stat, min_edge=args.min_edge)
    for p in result["props"]:
        print(f"  [{p['tier']}] {p['player_name']} {p['stat_type']} o{p['line']} "
              f"-- model {p['model_prob']:.1%} vs market {p['market_prob_over']:.1%} "
              f"(edge {p['edge']:+.1%}, {p['n_books']} books)")
    for b in result["bait"]:
        print(f"  [{b['trap_label']}] {b['player_name']} -- {b['bait']}; "
              + "; ".join(b["hooks"])
              + (f" (model: {b['model_prob']:.1%} to repeat)" if b["model_prob"] is not None else ""))
