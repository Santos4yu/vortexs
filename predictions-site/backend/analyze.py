"""
Vortex — Bet Slip Analyzer
===========================
Powers /analyze: AI vision → native stats math → grade → embed.

Flow
----
  1. extract_slip_data()   Gemini vision API reads the image as structured JSON
  2. normalize_market()    Maps raw market string to canonical prop_type key
  3. compute_hit_rates()   Native Python game-log loop — MLB Stats API (free)
  4. grade_pick()          Point-score algorithm → Elite / Strong / Good / Lean
  5. get_matchup_info()    Schedule API → opposing pitcher + BvP (free)
  6. build_analyze_embed() Formatted Discord Embed

Vision provider: OCR.space (free) + Groq Llama 3.3 70B (free) for intelligent text parsing.
Fallback: Regex-based parsing if Groq is unavailable.
"""

import re
import os
import io
import json
import base64
import struct
import zlib
import statistics
import aiohttp
import discord
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

import stats_mlb

try:
    from PIL import Image
except Exception:
    Image = None

try:
    from groq import Groq as _GroqClient
    _GROQ_KEY = os.getenv("GROQ_API_KEY", "")
    if _GROQ_KEY:
        _GROQ_CLIENT = _GroqClient(api_key=_GROQ_KEY)
    else:
        _GROQ_CLIENT = None
except Exception:
    _GROQ_CLIENT = None

OCR_API_KEY    = os.getenv("OCR_API_KEY", "helloworld")
_OCR_ENDPOINT  = "https://api.ocr.space/parse/image"


def _detect_green_selection(image_bytes: bytes) -> str | None:
    """
    Detect the selected bet side from green pixel highlighting.
    Works for any sportsbook that highlights the selected option in green
    (PrizePicks, DraftKings, FanDuel, BetMGM, etc.).
    Returns "over" or "under" based on which half of the image has more green.
    """
    pixels = None
    w = h = 0
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            w, h = img.size
            pixels = lambda x, y: img.getpixel((x, y))
        except Exception:
            pixels = None

    if pixels is None:
        png = _read_png_rgb(image_bytes)
        if png:
            w, h, data = png
            pixels = lambda x, y: data[y][x]

    if pixels is None or w < 40 or h < 40:
        return None

    green_pixels = []
    # The buttons live on the right side of a PrizePicks card. Sampling there
    # avoids player-headshot colors and keeps the scan cheap.
    x0 = int(w * 0.45)
    for y in range(h):
        for x in range(x0, w):
            r, g, b = pixels(x, y)
            if g >= 180 and r <= 140 and b <= 90 and g >= r + 70 and g >= b + 90:
                green_pixels.append((x, y))

    if len(green_pixels) < 20:
        return None

    # Count green pixels in top vs bottom half.
    # On PrizePicks, "More" is the upper button and "Less" is the lower button
    # within EACH card.  For multi-prop images, we check which half of the
    # image has more green — the selected button is always in the upper
    # position of its card, so the half with more green wins.
    top_count = sum(1 for _, y in green_pixels if y < h * 0.50)
    bot_count = len(green_pixels) - top_count

    if top_count == 0 and bot_count == 0:
        return None

    # "More" = upper button → "over".  "Less" = lower button → "under".
    return "over" if top_count >= bot_count else "under"


def _read_png_rgb(image_bytes: bytes):
    """Tiny stdlib PNG reader for 8-bit RGB/RGBA non-interlaced screenshots."""
    if image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    width = height = color_type = bit_depth = None
    compressed = bytearray()
    try:
        while pos + 8 <= len(image_bytes):
            length = struct.unpack(">I", image_bytes[pos:pos + 4])[0]
            ctype = image_bytes[pos + 4:pos + 8]
            chunk = image_bytes[pos + 8:pos + 8 + length]
            pos += 12 + length
            if ctype == b"IHDR":
                width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
                if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
                    return None
            elif ctype == b"IDAT":
                compressed.extend(chunk)
            elif ctype == b"IEND":
                break
        if not width or not height:
            return None
        channels = 4 if color_type == 6 else 3
        stride = width * channels
        raw = zlib.decompress(bytes(compressed))
        rows = []
        prev = [0] * stride
        idx = 0
        for _ in range(height):
            filt = raw[idx]
            idx += 1
            scan = list(raw[idx:idx + stride])
            idx += stride
            recon = [0] * stride
            for i, val in enumerate(scan):
                left = recon[i - channels] if i >= channels else 0
                up = prev[i]
                up_left = prev[i - channels] if i >= channels else 0
                if filt == 0:
                    out = val
                elif filt == 1:
                    out = val + left
                elif filt == 2:
                    out = val + up
                elif filt == 3:
                    out = val + ((left + up) // 2)
                elif filt == 4:
                    p = left + up - up_left
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                    pr = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                    out = val + pr
                else:
                    return None
                recon[i] = out & 255
            rows.append([tuple(recon[i:i + 3]) for i in range(0, stride, channels)])
            prev = recon
        return width, height, rows
    except Exception:
        return None

# ── Known MLB team abbreviations for token filtering ─────────────────────────
_MLB_TEAMS = {
    "ARI","AZ","ATL","BAL","BOS","CHC","CWS","CIN","CLE","COL",
    "DET","HOU","KC","LAA","LAD","MIA","MIL","MIN","NYM","NYY",
    "OAK","PHI","PIT","SD","SEA","SF","STL","TB","TEX","TOR","WSH",
}

# Position strings to exclude from team detection
_POSITIONS = {"IF","OF","DH","1B","2B","3B","SS","LF","CF","RF","C","P","SP","RP","CP"}

# ── Park factors (2025) ───────────────────────────────────────────────────────
# Keyed by HOME team abbreviation.  >1.0 = hitter-friendly, <1.0 = pitcher-friendly.
PARK_FACTORS: dict[str, float] = {
    "COL": 1.15, "BOS": 1.08, "CIN": 1.07, "PHI": 1.06, "TEX": 1.05,
    "MIL": 1.04, "ATL": 1.03, "BAL": 1.02, "NYY": 1.02, "CWS": 1.01,
    "CHC": 1.01, "ARI": 1.01, "WSH": 1.01, "HOU": 1.00, "LAD": 1.00,
    "MIN": 1.00, "TOR": 1.00, "KC":  1.00, "LAA": 1.00, "STL": 0.98,
    "DET": 0.98, "SF":  0.97, "OAK": 0.97, "TB":  0.97, "NYM": 0.96,
    "SD":  0.96, "MIA": 0.96, "CLE": 0.96, "PIT": 0.96, "SEA": 0.95,
}

# ── Market normalization ─────────────────────────────────────────────────────

_SEP = r"(?:\s*(?:[+&,]|and)\s*|\s+)"   # +, &, comma, "and", or plain space

# OCR often reads capital "I" in "RBIs" as lowercase "l" → r[bB][iIl1][sS]?
# Also catches no-separator runs: "HitsRunsRBIs", abbreviations "H+R+RBI"
_HRR = (
    rf"hits?{_SEP}runs?{_SEP}r[bB][iIl1][sS]?"   # "Hits Runs RBls" / "Hits+Runs+RBIs"
    rf"|h\s*[+&]?\s*r\s*[+&]?\s*r[bB][iIl1][sS]?"  # "H+R+RBI" / "H R RBI"
    rf"|hrr[bi]?"                                   # "HRR" / "HRRBI"
    rf"|hits?runs?r[bB][iIl1]"                      # "HitsRunsRBI" (no sep)
)

# Broad catch-all: if both "hit(s)" AND "rbi" appear anywhere in the window,
# it's H+R+RBI — OCR can't produce that combination for any other prop.
_HRR_BROAD = r"(?=.*\bhits?\b)(?=.*\br[bB][iIl1][sS]?\b)"

_MARKET_PATTERNS = [
    # H+R+RBI — must be FIRST; multiple variants to survive OCR degradation
    (_HRR,                                                                         "hits_runs_rbis"),
    (_HRR_BROAD,                                                                   "hits_runs_rbis"),
    # ── WNBA (basketball) — combos longest-first; matches both full words
    # ("Points Rebounds Assists") and slip abbreviations ("Pts+Reb+Ast").
    # Full words won't collide with MLB slips (no points/rebounds/assists). ────
    (r"(?:points?|pts).*(?:rebounds?|rebs?).*(?:assists?|ast)|(?<!\w)pra(?!\w)",    "pts_reb_ast"),
    (r"(?:points?|pts).*(?:rebounds?|rebs?)|(?<!\w)pr(?!\w)",                       "pts_reb"),
    (r"(?:points?|pts).*(?:assists?|ast)|(?<!\w)pa(?!\w)",                          "pts_ast"),
    (r"(?:rebounds?|rebs?).*(?:assists?|ast)|(?<!\w)ra(?!\w)",                      "reb_ast"),
    (r"three\s*-?\s*pointers?(\s*made)?|3\s*-?\s*pointers?|(?<!\w)3pm?(?!\w)|(?<!\w)threes(?!\w)", "threes"),
    (r"(?<!\w)points?(?!\w)|(?<!\w)pts(?!\w)",                                      "points"),
    (r"(?<!\w)rebounds?(?!\w)|(?<!\w)rebs?(?!\w)",                                  "rebounds"),
    (r"(?<!\w)assists?(?!\w)|(?<!\w)ast(?!\w)",                                     "assists"),
    (r"passes?\s*attempted",                                                        "passes_attempted"),
    (r"total\s*bases?|(?<!\w)tb(?!\w)",                                            "total_bases"),
    (r"home\s*runs?|(?<!\w)hr(?!\w)",                                              "home_runs"),
    # Pitcher-specific patterns MUST come before generic single-word patterns
    # to avoid "Hits Allowed" matching "hits", "Earned Runs Allowed" matching "runs", etc.
    (r"pitcher\s*outs?|\b(?:pitching\s*)?outs?(?:recorded)?\b|(?<!\w)po(?!\w)",  "pitcher_outs"),
    (r"hits\s*allowed|pitcher\s*hits|(?<!\w)ha(?!\w)",                              "pitcher_hits_allowed"),
    (r"earned\s+runs?\s*allowed|(?:earned\s*)?runs?\s*allowed|pitcher\s*er|(?<!\w)era(?!\w)", "pitcher_earned_runs"),
    (r"runs?\s*scored",                                                             "runs_scored"),
    (r"pitcher\s*strikeouts?|strikeouts?|(?<!\w)ks?(?!\w)",                        "strikeouts"),
    (r"(?<!\w)r[bB][iIl1][sS]?(?!\w)",                                            "rbis"),
    (r"(?<!\w)walks?|(?<!\w)bb(?!\w)",                                             "walks"),
    (r"(?<!\w)runs?(?!\w)",                                                        "runs_scored"),
    (r"(?<!\w)hits?(?!\w)",                                                        "hits"),
    (r"fantasy\s*score|pp\s*fantasy|prizepicks?\s*fantasy|hitter\s*fs|(?<!\w)fs(?!\w)", "fantasy_score"),
]

_MARKET_DISPLAY = {
    "hits_runs_rbis": "Hits+Runs+RBIs",
    "total_bases":    "Total Bases",
    "home_runs":      "Home Runs",
    "hits":           "Hits",
    "rbis":           "RBIs",
    "runs_scored":    "Runs Scored",
    "strikeouts":     "Strikeouts",
    "walks":          "Walks",
    "passes_attempted": "Passes Attempted",
    "fantasy_score":  "Fantasy Score (PP)",
    "pitcher_outs":   "Outs",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_earned_runs": "Earned Runs",
    "points":         "Points",
    "rebounds":       "Rebounds",
    "assists":        "Assists",
    "pts_reb_ast":    "Pts + Reb + Ast",
    "pts_reb":        "Pts + Reb",
    "pts_ast":        "Pts + Ast",
    "reb_ast":        "Reb + Ast",
    "threes":         "3-Pointers Made",
}


def normalize_market(raw: str) -> str:
    """Map any freeform market string to a canonical prop_type key via regex."""
    for pattern, canonical in _MARKET_PATTERNS:
        if re.search(pattern, raw.strip(), re.IGNORECASE):
            return canonical
    return "hits_runs_rbis"


def _read_side_from_text(raw_text: str) -> str | None:
    """
    Detect OVER/UNDER direction from bet slip OCR text.
    Handles ALL sportsbooks: PrizePicks, DraftKings, FanDuel, Underdog, BetMGM, etc.

    Returns "over", "under", or None.
    """
    return detect_direction(raw_text)["side"]


def detect_direction(text: str) -> dict:
    """
    Detect prop direction from any text input.
    Returns {"side": "over"|"under"|None, "source": str, "confidence": float}.
    """
    over_kw   = r'over|more|higher|high|up|above'
    under_kw  = r'under|less|lower|low|down|below'
    arrow_up  = r'[↑⬆▲]'
    arrow_dn  = r'[↓⬇▼]'
    result = {"side": None, "source": None, "confidence": 0.0}

    # 1. "Over 4.5" / "Under 2.5" — explicit word + number (FanDuel, BetMGM, generic)
    m = re.search(rf'\b({over_kw})\s+(\d+(?:\.\d+)?)\b', text, re.IGNORECASE)
    if m:
        return {"side": "over", "source": "explicit_word", "confidence": 0.99}
    m = re.search(rf'\b({under_kw})\s+(\d+(?:\.\d+)?)\b', text, re.IGNORECASE)
    if m:
        return {"side": "under", "source": "explicit_word", "confidence": 0.99}

    # 2. "O 4.5" / "U 2.5" — single-letter shorthand (DraftKings Pick6, etc.)
    m = re.search(r'\bO\s*(\d+(?:\.\d+)?)\b', text)
    if m:
        return {"side": "over", "source": "o_shorthand", "confidence": 0.95}
    m = re.search(r'\bU\s*(\d+(?:\.\d+)?)\b', text)
    if m:
        return {"side": "under", "source": "u_shorthand", "confidence": 0.95}

    # 3. Standalone keywords (no number required)
    # Must use word boundaries to avoid "Furthermore" → "More", "Value" → "al"
    if re.search(rf'(?<!\w)({over_kw})(?!\w)', text, re.IGNORECASE):
        return {"side": "over", "source": "keyword", "confidence": 0.90}
    if re.search(rf'(?<!\w)({under_kw})(?!\w)', text, re.IGNORECASE):
        return {"side": "under", "source": "keyword", "confidence": 0.90}

    # 4. Arrow symbols (may or may not be near a number)
    if re.search(arrow_up, text):
        return {"side": "over", "source": "arrow", "confidence": 0.85}
    if re.search(arrow_dn, text):
        return {"side": "under", "source": "arrow", "confidence": 0.85}

    return result


def detect_direction_per_region(prop_name: str, line_value: float, full_text: str) -> dict:
    """
    Detect direction for a single prop by searching the OCR text near that prop's
    player name and line value. Returns {"side", "source", "confidence"}.

    For multi-prop images, each prop gets its own detection run against the text
    surrounding its player name.
    """
    lines = full_text.split('\n')
    prop_name_lower = prop_name.lower().strip()

    # Find lines containing this player's name
    name_indices = []
    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        # Match on last name or full name
        parts = prop_name_lower.split()
        if len(parts) >= 2:
            # Match if both first and last name appear in this line
            if parts[-1] in line_lower and parts[0] in line_lower:
                name_indices.append(i)
        elif parts[0] in line_lower:
            name_indices.append(i)

    if not name_indices:
        # Can't find player name in text — fall back to full text scan
        return detect_direction(full_text)

    # Build a window of ±3 lines around the player name
    best = {"side": None, "source": None, "confidence": 0.0}
    for idx in name_indices:
        start = max(0, idx - 3)
        end = min(len(lines), idx + 4)
        window = '\n'.join(lines[start:end])
        d = detect_direction(window)
        if d["side"] and d["confidence"] > best["confidence"]:
            best = d

    if best["side"]:
        return best

    # Also scan for line value near the player name
    line_str = str(line_value)
    for idx in name_indices:
        start = max(0, idx - 3)
        end = min(len(lines), idx + 4)
        window = '\n'.join(lines[start:end])
        if line_str in window:
            # Found the line value near this player — scan that window for direction
            return detect_direction(window)

    return best


# ── 1. OCR.space image extraction ────────────────────────────────────────────

async def extract_slip_data(image_bytes: bytes) -> dict:
    """
    Read bet slip image via OCR.space, then parse with Groq LLM.
    Falls back to regex parsing if Groq is unavailable.

    Returns {"player_name", "team", "market_raw", "line", "side", "prop_type"}
    on success, or {"error": <message>} on failure.
    """
    # Detect MIME type from magic bytes
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        mime = "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        mime = "image/jpeg"
    elif len(image_bytes) > 12 and image_bytes[8:12] == b'WEBP':
        mime = "image/webp"
    else:
        mime = "image/png"

    # Step 1: OCR the image
    raw_text = await _ocr_image(image_bytes, mime)
    if isinstance(raw_text, dict):
        return raw_text  # error dict
    print(f"[analyze] OCR OK len={len(raw_text)}")

    # Step 2: Parse with Groq LLM (better than regex)
    if _GROQ_CLIENT is not None:
        result = await _groq_parse_text(raw_text)
        if result and "error" not in result:
            # Validate and fill sides per-prop
            _validate_sides(result, raw_text, image_bytes)
            result["_ocr_raw"] = raw_text
            _props_log = result.get("all_props") or [result]
            for _p in _props_log:
                print(f"[analyze] FINAL player={_p.get('player_name','?')} side={(_p.get('side') or 'NONE').upper()} line={_p.get('line')} market={_p.get('market_raw','?')}")
            return result

    # Step 3: Fallback to regex parsing
    print("[analyze] Groq unavailable — using regex fallback")
    result = _parse_slip_text(raw_text)
    if isinstance(result, dict) and "error" not in result:
        _validate_sides(result, raw_text, image_bytes)
    if isinstance(result, dict):
        result["_ocr_raw"] = raw_text
        _props_log = result.get("all_props") or [result]
        for _p in _props_log:
            print(f"[analyze] FINAL player={_p.get('player_name','?')} side={(_p.get('side') or 'NONE').upper()} line={_p.get('line')} market={_p.get('market_raw','?')}")
    return result


def _validate_sides(result: dict, raw_text: str, image_bytes: bytes) -> None:
    """
    Validate and fill side for each prop using a priority chain:
    1. Already set by Groq (from OCR text analysis)
    2. Text detection per-prop region
    3. Text detection on full OCR text
    4. Visual green-pixel detection as last resort

    Modifies result in place. Sets _side_source and _side_confidence.
    """
    props = result.get("all_props") or [result]

    for p in props:
        name = p.get("player_name", "?")
        line = p.get("line", 0)

        if p.get("side") and p["side"] in ("over", "under"):
            src = p.get("_side_source", "groq")
            print(f"[side-detect] {name} {line} = {p['side'].upper()} (source={src})")
            continue

        # Try per-prop text detection
        td = detect_direction_per_region(name, line, raw_text)
        if td["side"]:
            p["side"] = td["side"]
            p["_side_source"] = f"region:{td['source']}"
            p["_side_confidence"] = td["confidence"]
            print(f"[side-detect] {name} {line} = {td['side'].upper()} (source=region:{td['source']}, conf={td['confidence']})")
            continue

        # Try full-text detection
        fd = detect_direction(raw_text)
        if fd["side"]:
            p["side"] = fd["side"]
            p["_side_source"] = f"fulltext:{fd['source']}"
            p["_side_confidence"] = fd["confidence"]
            print(f"[side-detect] {name} {line} = {fd['side'].upper()} (source=fulltext:{fd['source']}, conf={fd['confidence']})")
            continue

        # Last resort: visual green pixel detection
        visual = _detect_green_selection(image_bytes)
        if visual:
            p["side"] = visual
            p["_side_source"] = "visual:green_pixel"
            p["_side_confidence"] = 0.70
            print(f"[side-detect] {name} {line} = {visual.upper()} (source=visual:green_pixel, conf=0.70)")
            continue

        # Hard gate: no direction detected — mark as error
        p["side"] = None
        p["_side_source"] = "none"
        p["_side_confidence"] = 0.0
        print(f"[side-detect] {name} {line} = NONE (source=none)")


async def _ocr_image(image_bytes: bytes, mime: str) -> str | dict:
    """Send image to OCR.space and return raw text or error dict."""
    b64      = base64.standard_b64encode(image_bytes).decode()
    data_uri = f"data:{mime};base64,{b64}"

    form = {
        "apikey":            OCR_API_KEY,
        "base64Image":       data_uri,
        "language":          "eng",
        "OCREngine":         "2",
        "detectOrientation": "true",
        "scale":             "true",
        "isTable":           "false",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _OCR_ENDPOINT, data=form,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"error": f"OCR API returned {resp.status}: {body[:150]}"}
                result = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        return {"error": f"Network error reaching OCR API: {exc}"}

    if result.get("IsErroredOnProcessing"):
        msgs = result.get("ErrorMessage") or ["Unknown OCR error"]
        return {"error": f"OCR failed: {msgs[0] if isinstance(msgs, list) else msgs}"}

    pages = result.get("ParsedResults") or []
    if not pages:
        return {"error": "OCR returned no text. Try a clearer screenshot."}

    raw_text = "\n".join(p.get("ParsedText", "") for p in pages)
    if not raw_text.strip():
        return {"error": "OCR found no readable text in the image."}
    return raw_text


async def _groq_parse_text(raw_text: str) -> dict | None:
    """Use Groq LLM to intelligently parse OCR text into structured prop data."""
    prompt = f"""You are a sports betting slip parser. Given the raw OCR text from a bet slip, extract ALL player props and detect which side (OVER or UNDER) is SELECTED for each.

OCR TEXT:
\"\"\"
{raw_text}
\"\"\"

Return a JSON array of props. Each prop object must have exactly these keys:
- "player_name": Full player name (e.g. "Shohei Ohtani")
- "team": Team abbreviation if visible (e.g. "LAD"), empty string if not
- "market_raw": The stat type as shown. MLB examples: "Hits", "Total Bases", "Hits+Runs+RBIs", "Strikeouts", "Earned Runs", "Pitching Outs", "Home Runs". WNBA examples: "Points", "Rebounds", "Assists", "Pts+Reb+Ast", "Pts+Reb", "Pts+Ast", "Reb+Ast", "3-Pointers Made"
- "line": The numeric line value as a float (e.g. 1.5, 4.5)
- "side": The SELECTED direction — "over" or "under"

Rules for detecting the selected side:
- "More" = over, "Less" = under (PrizePicks)
- "Over" = over, "Under" = under (FanDuel, BetMGM, generic)
- "O" = over, "U" = under (DraftKings shorthand, e.g. "O 4.5" or "U 1.5")
- Arrows: ↑ or ⬆️ = over, ↓ or ⬇️ = under
- Green/highlighted button = selected side
- Higher/Up = over, Lower/Down = under
- Look at which option is visually SELECTED (highlighted, colored, bold) vs the unselected one
- For multi-leg parlays, each prop may have a DIFFERENT side — detect each independently
- NEVER guess the side. If you cannot determine it from the text, set "side" to null

Return format: [{{"player_name": "...", "team": "...", "market_raw": "...", "line": 1.5, "side": "over"}}]"""

    _MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    response = None
    for model in _MODELS:
        for attempt in range(2):
            try:
                response = _GROQ_CLIENT.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=1024,
                )
                print(f"[groq] OK model={model}")
                break
            except Exception as exc:
                print(f"[groq] model={model} attempt={attempt+1} failed: {exc}")
                if attempt == 0:
                    import time; time.sleep(1)
        if response is not None:
            break

    if response is None:
        print("[groq] ALL MODELS FAILED — falling back to regex")
        return None

    try:
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        props = json.loads(text)
        if not isinstance(props, list) or not props:
            return None

        for p in props:
            if "error" in p:
                return {"error": p["error"]}
            market = p.get("market_raw", "")
            p["prop_type"] = normalize_market(market) or "hits_runs_rbis"
            p["market_raw"] = _MARKET_DISPLAY.get(p["prop_type"], market)
            try:
                p["line"] = float(p["line"])
            except (ValueError, TypeError):
                p["line"] = 1.5

            # Normalize side from LLM output
            side = (p.get("side") or "").lower().strip()
            if side in ("over", "more", "higher", "above", "up"):
                p["side"] = "over"
            elif side in ("under", "less", "lower", "below", "down"):
                p["side"] = "under"
            else:
                # LLM couldn't determine — try text detection near this prop
                p["side"] = None

        # Validate sides per-prop: if LLM missed one, try text detection
        for p in props:
            if p.get("side") is None:
                name = p.get("player_name", "")
                line = p.get("line", 0)
                td = detect_direction_per_region(name, line, raw_text)
                if td["side"]:
                    p["side"] = td["side"]
                    p["_side_source"] = f"text:{td['source']}"
                    p["_side_confidence"] = td["confidence"]

        first = dict(props[0])
        if len(props) > 1:
            first["all_props"] = props
        else:
            first["all_props"] = None
        return first

    except Exception as exc:
        print(f"[groq] parse failed: {exc}")
        return None


# ── 2. Regex parser — extracts props from OCR raw text ───────────────────────

_OVER_WORDS  = r'over|more|higher|above|[↑⬆▲]'
_UNDER_WORDS = r'under|less|lower|below|[↓⬇▼]'
_UNDER_SET   = {"under", "less", "lower", "below", "↓", "⬇", "▼"}
_SIDE_PAT    = rf'\b({_OVER_WORDS}|{_UNDER_WORDS})\s+(\d{{1,2}}(?:\.\d)?)\b'
_SIDE_RE     = re.compile(_SIDE_PAT, re.IGNORECASE)

# PrizePicks layout: "N Stat\nMore\nLess" — number precedes the stat keyword.
_PP_LINE_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s+'
    r'(?:Ks?|strikeouts?|hits?|home\s*runs?|total\s*bases?|rbis?|walks?|runs?(?:\s*scored)?)',
    re.IGNORECASE,
)

# Matches "Firstname Lastname" or "Firstname M. Lastname" or "Name Jr./Sr./II/III"
# Anchored at START only so trailing team/position tokens don't block the match.
_NAME_RE = re.compile(
    r'^([A-Z][a-záéíóúàèìòù\-\']+(?:\s[A-Z]\.)?'   # First [M.]
    r'(?:\s[A-Z][a-záéíóúàèìòù\-\'\.]+){1,3}'       # Last [Last2] [suffix]
    r'(?:\s(?:Jr|Sr|II|III|IV)\.?)?)$',              # Optional suffix
    re.IGNORECASE,
)

# Words that definitively mean a line is NOT a player name
_SKIP_WORDS = {
    "over","under","hits","runs","rbis","total","bases","home",
    "strikeouts","walks","more","less","higher","lower","line",
    "pick","mlb","nba","nfl","away","vs","at","the","and","for",
    "sun","mon","tue","wed","thu","fri","sat","am","pm",
    "today","tonight","parlay","legs","selections","selected",
    "player","props","game","stats","prop","bet","slip",
}

# Trailing tokens OCR may append to a player name line: DK button labels,
# league badges, position codes, team abbreviations.  Stripped in a loop.
_TRAILING_JUNK = re.compile(
    r'\s+(?:More|Less|Over|Under|MLB|NBA|NFL|NHL|'
    r'OF|IF|DH|1B|2B|3B|SS|LF|CF|RF|SP|RP|CP|'
    r'[A-Z]{2,4}|[A-Z])\s*$',
    re.IGNORECASE,
)


def _extract_name_from_line(ln: str) -> str | None:
    """
    Pull a player name from a line that may have trailing team/position/button tokens.

    OCR often reads DK button labels ("More", "Less") or league badges ("MLB")
    on the same line as the player name.  We strip all trailing junk tokens in
    a loop so "Shohei Ohtani MLB LAD P" → "Shohei Ohtani".
    """
    # First cut at any separator character (·, •, -, |)
    clean = re.sub(r'\s*[·•\|]\s*.*$', '', ln).strip()

    # Loop: strip one trailing junk token per pass (up to 8 passes)
    for _ in range(8):
        prev = clean
        clean = _TRAILING_JUNK.sub('', clean).strip()
        if clean == prev:
            break

    if len(clean.split()) >= 2 and _NAME_RE.match(clean):
        return clean
    # Fallback: raw line (no trailing tokens to worry about)
    if len(ln.split()) >= 2 and _NAME_RE.match(ln):
        return ln
    return None


def _detect_player_names(lines: list[str]) -> list[tuple[int, str]]:
    """Return (line_index, canonical_name) for every player-name line found."""
    found = []
    for i, ln in enumerate(lines):
        # Skip lines with digits (odds, dates, line values)
        if re.search(r'\d', ln):
            continue
        words = ln.split()
        if len(words) < 2:
            continue
        # Skip lines whose FIRST word is a skip word (like "More 1.5 Total Bases")
        if words[0].lower() in _SKIP_WORDS:
            continue
        name = _extract_name_from_line(ln)
        if name:
            found.append((i, name))
    return found


def _market_from_text(text: str) -> str | None:
    for pattern, canonical in _MARKET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return canonical
    return None


def _side_from_window(search_text: str):
    """
    Return (side, line_val) for a single prop window.

    Handles two layouts:
      PrizePicks: "N Stat\nMore\nLess"  — number before stat keyword
      DraftKings: "More N Stat" / "N Stat Less" — side word adjacent to number

    For PrizePicks where both More AND Less appear, the LAST side word in the
    text is treated as the selected (highlighted) button.
    """
    # ── PrizePicks primary path: number immediately before a stat keyword ──────
    pp = _PP_LINE_RE.search(search_text)
    if pp:
        lv = float(pp.group(1))
        # Collect all side words that appear after the stat keyword
        after      = search_text[pp.end():]
        after_hits = re.findall(rf'({_OVER_WORDS}|{_UNDER_WORDS})', after, re.IGNORECASE)
        if after_hits:
            # LAST occurrence = selected (highlighted) button
            chosen = after_hits[-1].lower()
            return ("under" if chosen in _UNDER_SET else "over"), lv
        # Check before the stat keyword too (e.g. "Less 4 Ks")
        before     = search_text[:pp.start()]
        before_hit = re.search(rf'({_OVER_WORDS}|{_UNDER_WORDS})', before, re.IGNORECASE)
        if before_hit:
            chosen = before_hit.group(1).lower()
            return ("under" if chosen in _UNDER_SET else "over"), lv
        return None, lv

    # ── DraftKings / Underdog path: side word adjacent to the number ───────────
    sl_iter = list(_SIDE_RE.finditer(search_text))
    if not sl_iter:
        return None, None

    first   = sl_iter[0]
    line_val = float(first.group(2))

    # Side word AFTER the line value = selected button on DK slips
    lv_str   = str(line_val)
    lv_pat   = re.compile(r'\b' + re.escape(lv_str) + r'\b')
    lv_match = lv_pat.search(search_text)
    if lv_match:
        after_text  = search_text[lv_match.end():]
        after_sides = re.findall(
            rf'\b({_OVER_WORDS}|{_UNDER_WORDS})\b', after_text, re.IGNORECASE
        )
        if after_sides:
            chosen = after_sides[0].lower()
            return ("under" if chosen in _UNDER_SET else "over"), line_val

    chosen = first.group(1).lower()
    return ("under" if chosen in _UNDER_SET else "over"), line_val


def _parse_all_props(lines: list[str]) -> list[dict]:
    """
    Parse every prop from a multi-player slip — no upper limit on prop count.

    DraftKings / Underdog layout always puts the More/Less button and line
    value AFTER the player name, never before it.  So the window starts at
    the player's own name line — no back window — guaranteeing we never
    bleed the previous player's market/side into the current player's data.

    Window: [player name line] → [next player name line, exclusive]
    Side detection uses _side_from_window() to handle the DK "unselected
    button BEFORE line value, selected button AFTER line value" pattern.
    """
    name_hits = _detect_player_names(lines)
    if not name_hits:
        return []

    props = []
    for idx, (line_idx, player_name) in enumerate(name_hits):
        # Strict forward-only window: name line → next player's name line
        next_name_idx = name_hits[idx + 1][0] if idx + 1 < len(name_hits) else len(lines)

        window_lines = lines[line_idx:next_name_idx]
        search_text  = " ".join(window_lines)

        # ── Side + line (DK-aware) ────────────────────────────────────────────
        side, line_val = _side_from_window(search_text)
        if line_val is None:
            continue  # can't grade without a line value + direction

        # ── Market (scan this player's window only) ───────────────────────────
        prop_type = _market_from_text(search_text) or "hits_runs_rbis"

        # ── Team ──────────────────────────────────────────────────────────────
        team = ""
        for c in re.findall(r'\b([A-Z]{2,3})\b', search_text):
            if c in _MLB_TEAMS and c not in _POSITIONS:
                team = c
                break

        props.append({
            "player_name": player_name,
            "team":        team,
            "market_raw":  _MARKET_DISPLAY.get(prop_type, prop_type),
            "line":        line_val,
            "side":        side,
            "prop_type":   prop_type,
        })

    return props


def _parse_slip_text(text: str) -> dict:
    """
    Extract player_name / team / market / line from raw OCR output.

    Returns a single-prop dict (first prop found) plus an optional
    `all_props` list when multiple props are detected, so the caller can
    offer a select menu.
    """
    lines     = [ln.strip() for ln in text.splitlines() if ln.strip()]
    full_text = " ".join(lines)

    all_props = _parse_all_props(lines)

    if len(all_props) >= 2:
        # Return first prop as the default, but expose the full list
        first = all_props[0]
        first["all_props"] = all_props
        return first

    # Single-prop path — use the same DK-aware side detection
    side, line_val = _side_from_window(full_text)
    if side is None:
        side = "under" if re.search(rf'\b({_UNDER_WORDS})\b', full_text, re.IGNORECASE) else "over"
    if line_val is None:
        # Strip time-of-day patterns (e.g. "5:05 PM") before number search
        # so a game time is never mistaken for the prop line.
        text_clean = re.sub(r'\b\d{1,2}:\d{2}(?:\s*[AP]M)?\b', '', full_text, flags=re.IGNORECASE)
        m = re.search(r'\b(\d+(?:\.\d+)?)\b', text_clean)
        line_val = float(m.group(1)) if m else None

    prop_type = _market_from_text(full_text)

    team = ""
    for c in re.findall(r'\b([A-Z]{2,3})\b', full_text):
        if c in _MLB_TEAMS and c not in _POSITIONS:
            team = c
            break

    player_name = None
    for ln in lines:
        if re.search(r'\d', ln):
            continue
        words = ln.split()
        if len(words) < 2:
            continue
        if any(w.lower() in _SKIP_WORDS for w in words):
            continue
        if _NAME_RE.match(ln):
            player_name = ln
            break

    if not player_name:
        m = re.search(r'\b([A-Z][a-z]+\s[A-Z][a-z]+)\b', full_text)
        if m:
            player_name = m.group(1)

    if not player_name:
        return {"error": "Could not read a player name from the image. Try cropping a single prop card."}
    if line_val is None:
        return {"error": "Could not read the prop line value from the image."}
    if prop_type is None:
        prop_type = "hits_runs_rbis"

    return {
        "player_name": player_name,
        "team":        team,
        "market_raw":  _MARKET_DISPLAY.get(prop_type, prop_type),
        "line":        line_val,
        "side":        side,
        "prop_type":   prop_type,
        "all_props":   None,
    }


# ── 3. Native hit-rate math — MLB Stats API (free, no key) ───────────────────

def compute_hit_rates(player_id: int, line: float, prop_type: str) -> dict:
    """
    Pull the player's current-season game log from the MLB Stats API.
    Loop through each entry and compute exact hit counts for L5 / L10 / L20.

    For aggregate stats like hits_runs_rbis, the loop explicitly sums
    (hits + runs + rbis) per game before comparing to the line.
    See stats_mlb._stat_from_game() for the single-source computation.
    """
    return stats_mlb.get_historical_splits(player_id, line, prop_type)


# ── 4. Tonight's matchup (schedule API — free) ───────────────────────────────

def get_matchup_info(player_id: int) -> dict:
    """Find the team's NEXT upcoming game + opposing pitcher from the MLB schedule.
    Skips games that have already started/finished so a completed day game is never
    served as a live play. Scans board date → today → tomorrow → day-after, so once
    today's game is over (or today is an off day) it serves the next slate early for
    pre-game value instead of going dark."""
    team_id = stats_mlb.get_player_current_team(player_id)
    if not team_id:
        return {}

    from vortextime import vortex_board_day, vortex_day, vortex_day_offset
    from datetime import datetime as _dt, timezone as _tz

    _now = _dt.now(_tz.utc)

    def _is_over(game: dict) -> bool:
        """
        True once the game is actually done (MLB status "Final"), NOT merely
        once first pitch has passed. A live game is still tonight's real
        matchup for research purposes -- jumping ahead to tomorrow's
        probable starter the moment a game goes live showed the wrong
        pitcher for hours while the real game was still being played.
        Falls back to a start-time check only when status is missing
        entirely (defensive -- MLB's schedule endpoint always includes it).
        """
        status = (game.get("status") or "").lower()
        if status:
            return status == "final"
        game_utc = game.get("game_utc", "")
        if not game_utc:
            return False
        try:
            # No status at all AND started >6h ago -- treat as over rather
            # than showing a stale in-progress game indefinitely.
            return (_now - _dt.fromisoformat(game_utc.replace("Z", "+00:00"))).total_seconds() > 6 * 3600
        except (ValueError, TypeError):
            return False

    # Date order: a not-yet-final game on the earliest date wins, so a live
    # or upcoming game today is always preferred over tomorrow. Tomorrow/
    # day-after are fallbacks for when today's game is over or it's an off
    # day → early lines on the next slate.
    seen_dates = []
    for try_date in (vortex_board_day(), vortex_day(),
                     vortex_day_offset(1), vortex_day_offset(2)):
        if try_date in seen_dates:
            continue
        seen_dates.append(try_date)
        schedule = stats_mlb.get_todays_schedule(game_date=try_date)
        for game in schedule.values():
            g_utc = game.get("game_utc", "")
            if _is_over(game):
                continue   # game is actually final — not a live play anymore
            if team_id == game.get("home_team_id"):
                return {
                    "is_home":      True,
                    "opponent":     game.get("away_team_name", ""),
                    "pitcher":      game.get("away_pitcher"),
                    "pitcher_id":   game.get("away_pitcher_id"),
                    "home_team_id": team_id,
                    "opp_team_id":  game.get("away_team_id"),
                    "game_utc":     g_utc,
                }
            if team_id == game.get("away_team_id"):
                return {
                    "is_home":      False,
                    "opponent":     game.get("home_team_name", ""),
                    "pitcher":      game.get("home_pitcher"),
                    "pitcher_id":   game.get("home_pitcher_id"),
                    "home_team_id": game.get("home_team_id"),
                    "opp_team_id":  game.get("home_team_id"),
                    "game_utc":     g_utc,
                }
    return {}


def get_no_game_reason(player_id: int) -> str:
    """
    Explain why get_matchup_info() found no upcoming game, for a clear user message.

    Returns one of:
      "in_progress"  — team has a game TODAY whose first pitch has already passed
                       (live or final); nothing upcoming in the next few days.
      "off_day"      — no game found for this team across today→day-after (true gap
                       or next slate not posted yet).
      "unknown"      — couldn't resolve the team.
    """
    team_id = stats_mlb.get_player_current_team(player_id)
    if not team_id:
        return "unknown"

    from vortextime import vortex_day
    from datetime import datetime as _dt, timezone as _tz

    _now = _dt.now(_tz.utc)
    schedule = stats_mlb.get_todays_schedule(game_date=vortex_day())
    for game in schedule.values():
        if team_id not in (game.get("home_team_id"), game.get("away_team_id")):
            continue
        g_utc = game.get("game_utc", "")
        try:
            if g_utc and _dt.fromisoformat(g_utc.replace("Z", "+00:00")) <= _now:
                return "in_progress"   # today's game already started/finished
        except (ValueError, TypeError):
            pass
    return "off_day"


# ── 5. Algorithmic grading (pure arithmetic, zero API) ───────────────────────

def grade_pick(
    splits:      dict,
    line:        float,
    side:        str        = "over",
    opp_k_rank:  int | None = None,    # 1–30 rank (1 = hardest to K)
    opp_k_pct:   float | None = None,  # team K rate 0.0–1.0
    pitcher:     dict | None = None,   # from stats_mlb.get_pitcher_metrics()
    bvp:         dict | None = None,   # from stats_mlb.get_bvp_history() (vs pitcher)
    park_factor: float       = 1.0,    # from PARK_FACTOR[home_team_name]
    weather:     dict | None = None,   # from stats_mlb.get_game_weather()
    team_bvp:    dict | None = None,   # from stats_mlb.get_team_bvp()
    oaa:         dict | None = None,   # from stats_mlb.get_team_defense_oaa()
    prop_type:   str         = "",     # e.g. "strikeouts", "hits", "total_bases"
    lineup_spot: int | None  = None,   # today's batting order position (1-9)
    statcast:     dict | None = None,   # Barrel%, HH%, xSLG, xwOBA from Baseball Savant
    team_h2h:     dict | None = None,   # from stats_mlb.get_vs_team_splits()
    arsenal:       list | None = None,   # from stats_mlb.get_pitcher_arsenal()
    bat_vs_pitch:  list | None = None,   # from stats_mlb.get_batter_vs_pitch_type()
    vs_hand_splits: dict | None = None,  # from stats_mlb.get_batter_hand_splits() → {"L":{avg,ops,pa},"R":{...}}
    learned_weight: float | None = None, # composite multiplier from score_weights (1.0 = neutral)
    is_home:        bool | None  = None, # True = batter is at home tonight
    umpire:         dict | None  = None, # from stats_mlb.get_game_umpire() → {name, k_boost}
    opp_bullpen:    dict | None  = None, # from stats_mlb.get_team_bullpen() → {era, ops_against, ip}
) -> dict:
    """
    Point-score the pick then apply mandatory Risk Penalty Modifiers.

    Philosophy: matchup quality (pitcher, BvP, handedness) outweighs raw streaks.
    A strong matchup with a mediocre streak should outscore a hot streak vs an ace.

    Baseline points (form — necessary but not sufficient):
      +4   effective L10 ≥ 90%      dominant recent consistency
      +2   effective L10 ≥ 70%      solid recent consistency
      +2   effective L5  = 100%     red-hot last 5 games
      +1   effective L5  ≥ 80%      strong recent momentum (4/5)
      +2   effective L20 ≥ 75%      sustainable long-run base
      +2   L10 avg ≥ 1.5× line      significant stat gap vs line
      Pitcher ERA ladder (boosted — matchup is primary signal):
        ≤2.00 → +5 Under / -5 Over   elite ace suppression
        ≤3.00 → +2 Under / -2 Over   (unchanged from recent fix)
        ≤3.50 → +1 Under / -1 Over
        3.51–4.25 → 0 (neutral)
        4.26–5.00 → -1 Under / +1 Over
        5.01–6.00 → -3 Under / +3 Over
        ≥6.01  → -5 Under / +5 Over   disaster tier
      +1   pitcher FIP ≤ 3.0 (Under) / ≥ 5.0 (Over)
      +1   pitcher HR/9 ≤ 0.5 (Under) / ≥ 1.5 (Over, only when ERA ≥ 4.0)
      Pitch-mix fit (needs arsenal + bat_vs_pitch, ≥10% coverage, ≥5 PA/pitch):
        +2 Under / -2 Over   weighted OPS vs top 2 pitches < .550 (batter dominated)
        +1 Under / -1 Over   weighted OPS < .650 (below-avg vs pitch mix)
        -1 Under / +1 Over   weighted OPS > .750 (above-avg vs pitch mix)
        -2 Under / +2 Over   weighted OPS > .850 (batter crushes these pitches)
      Handedness OPS — boosted, direction-aware (≥20 PA required):
        +3 Under / -3 Over   batter OPS < .550 vs pitcher's hand
        +2 Under / -2 Over   batter OPS < .650 vs pitcher's hand
        -2 Under / +2 Over   batter OPS > .750 vs pitcher's hand
        -3 Under / +3 Over   batter OPS > .850 vs pitcher's hand
      BvP history — direction-aware, boosted (≥6 AB):
        Over:  +4 avg ≥ .333 · +2 avg ≥ .260 · -2 avg ≤ .200 · -3 avg ≤ .150
        Under: -3 avg ≥ .333 · -2 avg ≥ .260 · +2 avg ≤ .200 · +3 avg ≤ .150
      +2   park factor ≥ 1.08       very hitter-friendly park (Over)
      +1   park factor ≥ 1.04       hitter-friendly park (Over)
      +1   park factor ≤ 0.96       pitcher-friendly park (Under)

    Risk Penalty Modifiers (applied after baseline):
      −6   Matchup risk  — opponent rank 3–5 hardest to K (OR k_pct < 19%)
      −8   Matchup risk  — opponent rank 1–2 hardest to K (OR k_pct < 17.5%)
             penalties don't stack; worst single condition wins
      +3/+5 Matchup boost — opponent rank 26-30 easiest to K (OR k_pct > 22%)
             boost only for Over picks; penalty only for Under picks
      -6   Form risk     — effective L5 hit rate ≤ 40% (cold streak)
      -3   Form dip      — effective L5 hit rate ≤ 50% (mild cold)

    Hard cap rule:
      If TWO OR MORE risk flags trigger simultaneously, the label is
      force-capped at "Good" regardless of the final numeric score.
      A single flag only applies its point deduction with no label cap.
    """
    l5  = splits.get("l5")  or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}   # None → {} when < 10 games available
    has_l20 = bool(l20.get("games", 0))

    is_under = side.lower() == "under"

    # Raw over-side rates
    l5_rate_raw  = l5.get("rate",  0) or 0
    l10_rate_raw = l10.get("rate", 0) or 0
    l20_rate_raw = l20.get("rate", 0) or 0
    l10_avg      = l10.get("avg",  0) or 0

    # Effective rates (flip for Under)
    eff_l5  = (100 - l5_rate_raw)  if is_under else l5_rate_raw
    eff_l10 = (100 - l10_rate_raw) if is_under else l10_rate_raw
    eff_l20 = (100 - l20_rate_raw) if is_under else l20_rate_raw

    pitcher = pitcher or {}
    bvp     = bvp     or {}

    # ── Baseline score ────────────────────────────────────────────────────────
    has_l5_data  = bool(l5.get("games", 0))
    has_l10_data = bool(l10.get("games", 0))
    score = 0
    if has_l10_data:
        if   eff_l10 >= 90: score += 4   # reduced from 5 — streaks support, matchups decide
        elif eff_l10 >= 70: score += 2   # reduced from 3
    if has_l5_data:
        if   eff_l5 == 100: score += 2   # perfect recent run
        elif eff_l5 >= 80:  score += 1   # strong recent momentum (4/5)
    if   has_l20 and eff_l20 >= 75: score += 2   # only reward if data exists

    # ── Projection delta — how far L10 average sits vs the prop line ──────────
    # This is the strongest EV predictor: avg consistently above/below the line
    # means the market is mispriced regardless of hit-rate alone.
    proj_edge = 0.0
    if line > 0 and l10_avg:
        raw_edge = float(l10_avg) - float(line)
        proj_edge = -raw_edge if is_under else raw_edge   # positive = edge for our side
        if   proj_edge >= 1.5:  score += 3
        elif proj_edge >= 0.75: score += 2
        elif proj_edge >= 0.25: score += 1
        elif proj_edge <= -0.75: score -= 2
        elif proj_edge <= -0.25: score -= 1

    # ── Pitcher matchup ───────────────────────────────────────────────────────
    # ERA ladder — fully symmetric: good pitcher hurts Over / helps Under.
    # Under: low ERA → +pts (suppresses offense); high ERA → -pts (inflates HRR)
    # Over:  just inverted — facing an ace is a penalty, a bad pitcher is a bonus.
    if pitcher and not pitcher.get("error"):
        try:
            era = float(pitcher.get("era") or 0)
            hr9 = float(pitcher.get("hr_per_9") or 0)
            fip_raw = pitcher.get("fip")
            fip = float(fip_raw) if fip_raw else None

            # Small-sample guard: ERA needs ~50 IP to stabilize. On a tiny sample
            # an extreme ERA (e.g. 9.00 over 5 IP) is noise — prefer FIP if we have
            # it, and cap the ladder at ±2 so a fluke number can't max out the swing.
            try:
                _ip = float(str(pitcher.get("innings_pitched") or "0").split(".")[0])
            except (TypeError, ValueError):
                _ip = 0.0
            _era_reliable = _ip >= 25
            _era_for_ladder = era
            if not _era_reliable and fip:
                _era_for_ladder = fip   # FIP stabilizes faster — trust it on small samples
            _cap = 5 if _era_reliable else 2
            def _clamp(pts):
                return max(-_cap, min(_cap, pts))

            era = _era_for_ladder
            if is_under:
                if   0 < era <= 2.00: score += _clamp(5)  # elite ace — heavily rewards Under
                elif 0 < era <= 3.00: score += _clamp(2)
                elif 0 < era <= 3.50: score += 1
                # 3.51–4.25 → neutral
                elif 4.26 <= era < 5.01: score -= 1
                elif 5.01 <= era < 6.01: score -= _clamp(3)
                elif era >= 6.01:         score -= _clamp(5)
                if fip and fip <= 3.0: score += 1  # elite FIP confirms skill
                if 0 < hr9 <= 0.5:    score += 1  # barely allows extra bases
            else:
                if   0 < era <= 2.00: score -= _clamp(5)  # elite ace — heavily penalizes Over
                elif 0 < era <= 3.00: score -= _clamp(2)
                elif 0 < era <= 3.50: score -= 1
                # 3.51–4.25 → neutral
                elif 4.26 <= era < 5.01: score += 1
                elif 5.01 <= era < 6.01: score += _clamp(3)
                elif era >= 6.01:         score += _clamp(5)
                if hr9 >= 1.5 and era >= 4.0: score += 1  # high HR/9 only meaningful on bad pitchers
                if fip and fip >= 5.0:  score += 1
        except (TypeError, ValueError):
            pass

    # ── BvP history (≥4 AB required) — direction-aware, sample-scaled ────────
    # Penalty/boost scales with AB count — 11 AB at .000 is much more meaningful
    # than 4 AB at .000. Large samples (≥8 AB) use boosted tiers.
    bvp_ab = int(bvp.get("ab", 0) or 0)
    _bvp_scored_avg = 0.0   # stored for contradiction check below
    if bvp_ab >= 4:
        try:
            avg_str = str(bvp.get("avg") or ".000")
            _bvp_scored_avg = float("0" + avg_str) if avg_str.startswith(".") else float(avg_str)
            _big = bvp_ab >= 8   # 8+ AB = statistically meaningful — use stronger tiers
            if is_under:
                if   _bvp_scored_avg >= 0.333: score -= (5 if _big else 3)
                elif _bvp_scored_avg >= 0.260: score -= (3 if _big else 2)
                elif _bvp_scored_avg <= 0.150: score += (5 if _big else 3)
                elif _bvp_scored_avg <= 0.200: score += (3 if _big else 2)
            else:
                if   _bvp_scored_avg >= 0.333: score += (5 if _big else 4)
                elif _bvp_scored_avg >= 0.260: score += (3 if _big else 2)
                elif _bvp_scored_avg <= 0.150: score -= (6 if _big else 3)
                elif _bvp_scored_avg <= 0.200: score -= (3 if _big else 2)
        except (TypeError, ValueError):
            pass

    # ── Park factor (symmetric — a hitter park hurts Unders, a pitcher park hurts Overs) ─
    if not is_under:
        if   park_factor >= 1.08: score += 2   # hitter park boosts Over
        elif park_factor >= 1.04: score += 1
        elif park_factor <= 0.92: score -= 2   # extreme pitcher park undercuts Over
        elif park_factor <= 0.96: score -= 1
    else:
        if   park_factor <= 0.92: score += 2   # pitcher park boosts Under
        elif park_factor <= 0.96: score += 1
        elif park_factor >= 1.08: score -= 2   # extreme hitter park undercuts Under
        elif park_factor >= 1.04: score -= 1

    # ── Venue split (batter home/away hit rate) ───────────────────────────────
    # Uses last-20-game hit rate at tonight's venue. Adjusts ±1–2 points when
    # there's a meaningful gap between the two venues (≥4 games each).
    if is_home is not None:
        _h_rate  = splits.get("home_rate")
        _a_rate  = splits.get("away_rate")
        _h_games = splits.get("home_games", 0) or 0
        _a_games = splits.get("away_games", 0) or 0
        _venue_rate = (_h_rate if is_home else _a_rate)
        if _venue_rate is not None and _h_games >= 4 and _a_games >= 4:
            _eff_venue = (100 - _venue_rate) if is_under else _venue_rate
            if   _eff_venue >= 80: score += 2
            elif _eff_venue >= 65: score += 1
            elif _eff_venue <= 30: score -= 2
            elif _eff_venue <= 40: score -= 1

    # ── Opposing bullpen quality (batter props only) ──────────────────────────
    # Starters average ~5.2 IP, so a batter's last 1-2 PAs usually come against
    # relievers -- a bad pen adds real late-game production chances; an elite
    # pen takes them away. Reliever-only split, ≥50 IP enforced upstream.
    # Deliberately modest (±1/±2): it affects a minority of the batter's PAs.
    bullpen_score = 0
    _bp = opp_bullpen or {}
    if _bp.get("era") is not None and prop_type not in ("strikeouts", "pitcher_outs"):
        _bp_era = _bp["era"]
        if   _bp_era >= 4.70: bullpen_score = +2   # bottom-tier pen — late runs available
        elif _bp_era >= 4.30: bullpen_score = +1
        elif _bp_era <= 3.00: bullpen_score = -2   # elite pen — offense dies late
        elif _bp_era <= 3.40: bullpen_score = -1
        if is_under:
            bullpen_score = -bullpen_score          # what hurts the Over helps the Under
        score += bullpen_score

    # ── Wind / weather ────────────────────────────────────────────────────────
    weather = weather or {}
    if not weather.get("error") and not weather.get("dome"):
        speed = weather.get("speed_mph", 0) or 0
        hf    = weather.get("hitter_friendly")
        if hf is True and speed >= 10:
            score += 2 if speed >= 15 else 1
        elif hf is False and speed >= 10 and is_under:
            score += 1  # wind blowing in helps Under props too

    # ── Team BvP (career vs opposing team, all pitchers) ─────────────────────
    team_bvp = team_bvp or {}
    t_pa  = int(team_bvp.get("pa", 0) or 0)
    if t_pa >= 10:
        try:
            t_avg_str = str(team_bvp.get("avg", ".000") or ".000")
            t_avg = float("0" + t_avg_str) if t_avg_str.startswith(".") else float(t_avg_str)
            if   t_avg >= 0.320: score += 2
            elif t_avg >= 0.270: score += 1
            elif t_avg <= 0.180: score -= 1
        except (TypeError, ValueError):
            pass

    # ── Opponent defense OAA ──────────────────────────────────────────────────
    oaa = oaa or {}
    oaa_val = oaa.get("oaa")
    if oaa_val is not None and not is_under:
        if   oaa_val <= -10: score += 2   # very poor defense → more hits
        elif oaa_val <= -5:  score += 1

    # ── Team H2H prop history — how often has this prop gone Over/Under vs tonight's opponent ──
    # Minimum 5 games required to score. Ladder mirrors SILAS: scored from Over% perspective,
    # then inverted for Under picks so polarity is always correct.
    team_h2h = team_h2h or {}
    th_games = int(team_h2h.get("games", 0) or 0)
    if th_games >= 5:
        th_over_rate = float(team_h2h.get("over_rate", 50) or 50)
        if is_under:
            # High Over% vs this team = bad for Under
            if   th_over_rate >= 65: score -= 2
            elif th_over_rate >= 55: score -= 1
            elif th_over_rate <= 35: score += 2
            elif th_over_rate <= 45: score += 1
        else:
            # High Over% vs this team = good for Over
            if   th_over_rate >= 65: score += 2
            elif th_over_rate >= 55: score += 1
            elif th_over_rate <= 35: score -= 2
            elif th_over_rate <= 45: score -= 1

    # ── EPA — lineup spot → projected plate appearances ───────────────────────
    # Top of order gets ~4.4 PA, bottom gets ~3.6 PA. Only scores hitting props;
    # K props don't benefit from extra PA the same way batting stats do.
    _HITTING_PROPS = {"hits", "total_bases", "home_runs", "rbis",
                      "runs_scored", "hits_runs_rbis", "walks", "fantasy_score"}
    proj_pa = None
    if lineup_spot is not None and prop_type in _HITTING_PROPS:
        _PA_BY_SPOT = {1: 4.5, 2: 4.4, 3: 4.2, 4: 4.1, 5: 3.9,
                       6: 3.8, 7: 3.7, 8: 3.6, 9: 3.5}
        proj_pa = _PA_BY_SPOT.get(lineup_spot, 4.0)
        if   lineup_spot <= 2: score += 2   # leadoff/2-hole — maximum PA exposure
        elif lineup_spot <= 5: score += 1   # heart of order — above-avg PA
        elif lineup_spot >= 8: score -= 1   # bottom of order — fewer opportunities

    # ── Damage probability — Statcast power profile vs pitcher + environment ─────
    # Barrel%, HH%, xSLG, xwOBA were shown in embed but never scored — fixed here.
    # Only applies to props where extra-base hits drive the outcome.
    _POWER_PROPS = {"total_bases", "home_runs", "hits_runs_rbis",
                    "rbis", "runs_scored", "hits", "fantasy_score"}
    damage_score = 0
    if statcast and prop_type in _POWER_PROPS and not is_under:
        brl       = statcast.get("barrel_pct", 0)  or 0
        hh        = statcast.get("hard_hit_pct", 0) or 0
        xslg_raw  = str(statcast.get("xslg",  "") or "").strip()
        xwoba_raw = str(statcast.get("xwoba", "") or "").strip()

        if   brl >= 12: damage_score += 2   # elite barrel rate
        elif brl >= 8:  damage_score += 1   # above-avg barrel rate
        if   hh  >= 45: damage_score += 1   # hard-hit ≥ 45%

        try:
            xslg_f = float(xslg_raw)
            if   xslg_f >= 0.550: damage_score += 2   # power profile (SLG potential)
            elif xslg_f >= 0.450: damage_score += 1
        except (ValueError, TypeError):
            pass

        try:
            xwoba_f = float(xwoba_raw)
            if xwoba_f >= 0.380: damage_score += 1    # elite expected value / contact quality
        except (ValueError, TypeError):
            pass

        score += damage_score

    # ── Plate discipline — batter bat-to-ball reliability (real "Discipline Score") ─
    # Uses Statcast whiff% + chase% already merged into `statcast`. A contact-first
    # hitter keeps the bat on the ball → more balls in play → safer Over on hit-based
    # props. A free-swinger (high whiff/chase) carries empty-PA / strikeout risk →
    # favors Under. Only applied to hitter contact props with real Statcast values.
    # Conservative point swings (±1/±2) so it refines, never overrides, the matchup.
    _CONTACT_PROPS = {"hits", "hits_runs_rbis", "total_bases", "rbis", "runs_scored"}
    discipline_score = 0
    if statcast and prop_type in _CONTACT_PROPS:
        whiff = statcast.get("whiff_pct", 0) or 0   # league avg ≈ 24-25%
        chase = statcast.get("chase_pct", 0) or 0   # league avg ≈ 28-30%
        if whiff > 0:   # 0 = stat missing; never score a default
            if   whiff <= 18 and (chase <= 24 or chase == 0):
                discipline_score = +2   # elite contact — rarely whiffs or chases
            elif whiff <= 22:
                discipline_score = +1   # above-average bat-to-ball
            elif whiff >= 32 or chase >= 36:
                discipline_score = -2   # free-swinger — real strikeout/empty-PA risk
            elif whiff >= 28:
                discipline_score = -1   # below-average contact
            if is_under:
                discipline_score = -discipline_score   # contact safety helps Over, hurts Under
            score += discipline_score

    # ── Umpire zone — strike-zone size shifts K / contact balance ────────────────
    # k_boost is the ump's K%-vs-league delta (percentage points; ±3 = meaningful).
    # Big zone → more strikeouts & fewer walks → suppresses hitter offense (helps
    # Under) and inflates pitcher strikeouts (helps Over Ks). Small zone is the
    # mirror. ±1 only — a real but secondary environmental nudge.
    ump_score = 0
    _kb = (umpire or {}).get("k_boost")
    if _kb is not None:
        if prop_type == "strikeouts":
            if   _kb >= 3:  ump_score = -1 if is_under else +1   # big zone → more Ks
            elif _kb <= -3: ump_score = +1 if is_under else -1   # small zone → fewer Ks
        elif prop_type in _CONTACT_PROPS:
            if   _kb >= 3:  ump_score = +1 if is_under else -1   # big zone suppresses bats
            elif _kb <= -3: ump_score = -1 if is_under else +1   # small zone → more contact
        score += ump_score

    # ── Pitch-mix fit — batter's production vs pitcher's primary weapons ──────────
    # For each of the pitcher's top 2 pitches (by usage), look up the batter's OPS
    # vs that exact pitch type, then compute a usage-weighted average OPS.
    # Under: struggling vs top pitches confirms suppression beyond ERA alone.
    # Over:  crushing top pitches confirms offensive edge vs this arsenal.
    # Minimum: ≥10% total usage covered and ≥5 PA per pitch (pre-filtered by API call).
    pitch_mix_score = 0
    _arsenal     = arsenal      or []
    _bat_vs_pitch = bat_vs_pitch or []
    import logging as _logging
    _pmlog = _logging.getLogger("vortex.grade_pick")
    _pmlog.info("PITCH-MIX: arsenal=%d pitches  bat_vs_pitch=%d entries",
                len(_arsenal), len(_bat_vs_pitch))
    if _arsenal and _bat_vs_pitch:
        _bvp_map       = {r["pitch_type"]: r for r in _bat_vs_pitch}
        _total_weight  = 0.0
        _weighted_val  = 0.0
        _metric        = None   # "woba" or "ops" -- never mixed across pitches
        for pitch in _arsenal[:2]:
            pt  = pitch.get("pitch_type", "")
            pct = float(pitch.get("pct", 0) or 0)
            _pmlog.info("PITCH-MIX: checking pt=%s pct=%.1f in_map=%s", pt, pct, pt in _bvp_map)
            if not pt or pt not in _bvp_map:
                continue
            # Prefer wOBA (what Savant's per-pitch data provides; OPS isn't
            # published per pitch type). Fall back to OPS for legacy callers.
            row = _bvp_map[pt]
            use_woba = _metric != "ops" and str(row.get("woba", "") or "").strip() not in ("", ".---", "---")
            val_raw = str(row.get("woba" if use_woba else "ops", "") or "").strip()
            _pmlog.info("PITCH-MIX: pt=%s metric=%s raw=%r", pt, "woba" if use_woba else "ops", val_raw)
            if val_raw in ("", ".---", "---"):
                continue
            try:
                val_f = float("0" + val_raw) if val_raw.startswith(".") else float(val_raw)
                _weighted_val  += val_f * pct
                _total_weight  += pct
                _metric = "woba" if use_woba else "ops"
            except (ValueError, TypeError):
                pass
        _pmlog.info("PITCH-MIX: total_weight=%.1f weighted_val=%.3f metric=%s", _total_weight, _weighted_val, _metric)
        if _total_weight >= 10.0 and _metric:
            avg_val = _weighted_val / _total_weight
            # Same intent at both scales: league-avg wOBA ~.320 vs OPS ~.720,
            # so thresholds map (.260/.290/.350/.380) <-> (.550/.650/.750/.850).
            lo2, lo1, hi1, hi2 = (
                (0.260, 0.290, 0.350, 0.380) if _metric == "woba"
                else (0.550, 0.650, 0.750, 0.850)
            )
            _pmlog.info("PITCH-MIX: avg_val=%.3f  side=%s", avg_val, side)
            if is_under:
                if   avg_val < lo2: pitch_mix_score = +2
                elif avg_val < lo1: pitch_mix_score = +1
                elif avg_val > hi2: pitch_mix_score = -2
                elif avg_val > hi1: pitch_mix_score = -1
            else:
                if   avg_val > hi2: pitch_mix_score = +2
                elif avg_val > hi1: pitch_mix_score = +1
                elif avg_val < lo2: pitch_mix_score = -2
                elif avg_val < lo1: pitch_mix_score = -1
    _pmlog.info("PITCH-MIX: final pitch_mix_score=%+d", pitch_mix_score)
    score += pitch_mix_score

    # ── Handedness OPS — batter's production vs pitcher's throwing hand ───────────
    # Uses season-wide splits (≥20 PA required) so data is almost always present.
    # OPS vs the pitcher's actual hand is the most reliable proxy for pitch-mix fit
    # when per-pitch-type data isn't available from the API.
    # Thresholds vs league-average OPS (~.720):
    #   Under: low OPS vs pitcher hand → offense suppressed → confirms Under
    #   Over:  high OPS vs pitcher hand → offense favored  → confirms Over
    hand_ops_score = 0
    _ph = (pitcher or {}).get("hand", "")  # pitcher's throwing hand: "L" or "R"
    if _ph in ("L", "R") and vs_hand_splits:
        _hand_data = vs_hand_splits.get(_ph, {})
        _hand_pa   = int(_hand_data.get("pa", 0) or 0)
        if _hand_pa >= 20:
            ops_raw = str(_hand_data.get("ops", "") or "").strip()
            if ops_raw not in ("", ".---", "---"):
                try:
                    ops_f = float("0" + ops_raw) if ops_raw.startswith(".") else float(ops_raw)
                    if is_under:
                        if   ops_f < 0.550: hand_ops_score = +3
                        elif ops_f < 0.650: hand_ops_score = +2
                        elif ops_f > 0.850: hand_ops_score = -3
                        elif ops_f > 0.750: hand_ops_score = -2
                    else:
                        if   ops_f > 0.850: hand_ops_score = +3
                        elif ops_f > 0.750: hand_ops_score = +2
                        elif ops_f < 0.550: hand_ops_score = -3
                        elif ops_f < 0.650: hand_ops_score = -2
                except (ValueError, TypeError):
                    pass
    score += hand_ops_score

    # ── Variance / Stability — standard deviation of recent game values ─────────
    # Same hit rate with low variance = reliable; high variance = boom-or-bust.
    # Stability rewards consistent performers and penalizes spikers.
    # K props are naturally volatile (3K-8K range is normal) — wider thresholds.
    stability_tier = ""
    recent_vals = [
        float(g["value"]) for g in (splits.get("recent_games") or [])
        if isinstance(g.get("value"), (int, float))
    ]
    if len(recent_vals) >= 5:
        stdev = statistics.stdev(recent_vals) if len(recent_vals) > 1 else 0.0
        if prop_type == "strikeouts":
            # K props: wider bands since 3-8 K range is normal
            if   stdev < 1.0:  stability_tier = "HIGH";     score += 1
            elif stdev < 2.0:  stability_tier = "MEDIUM"
            elif stdev < 3.0:  stability_tier = "LOW";      score -= 1
            else:              stability_tier = "VOLATILE";  score -= 2
        else:
            if   stdev < 0.5:  stability_tier = "HIGH";     score += 1
            elif stdev < 1.0:  stability_tier = "MEDIUM"
            elif stdev < 2.0:  stability_tier = "LOW";      score -= 1
            else:              stability_tier = "VOLATILE";  score -= 2

    # ── Low-sample penalty ───────────────────────────────────────────────────
    # Prevents micro-sample "100% L10" from scoring like real data.
    # Skipped for strikeouts — _hr_k() already gates data quality (returns None
    # when < half the sample exists), so no data = no bonus AND no penalty.
    if prop_type != "strikeouts":
        l10_games = l10.get("games", 0) or 0
        if   l10_games < 5:  score -= 3   # < 5 games — stat is noise
        elif l10_games < 10: score -= 2   # < 10 games — limited depth
        elif not has_l20:    score -= 1   # < 20 games — below ideal depth

    # ── Learned weight modifier ──────────────────────────────────────────────
    # Per-dimension multipliers from score_weights table (hit_rate / 0.50).
    # Applied after baseline, before risk penalties, so risk flags still cap.
    if learned_weight is not None and learned_weight != 1.0:
        score = round(score * learned_weight)

    # ── Risk Penalty Modifiers ────────────────────────────────────────────────
    risk_flags   = []   # accumulate active flags for the hard-cap rule
    penalty_desc = []   # human-readable penalty lines for the embed

    # Flag 1 — Matchup risk/boost: only applies to strikeout props
    # A low team K-rate (hard to K) HURTS Over props but HELPS Under props.
    # A high team K-rate (K-prone) HURTS Under props but HELPS Over props.
    # MLB average K% ≈ 22-23%. Thresholds reflect that.
    if prop_type == "strikeouts":
        # Dead-zone fix: the 19-22% K% / rank 6-25 band previously produced ZERO
        # matchup signal. Now graduated into severe / moderate / mild tiers so most
        # lineups nudge the score in the correct direction.
        #   Hard to K (contact lineup):  rank ≤ 9  or K% < 20%
        #   K-prone   (free swingers):   rank ≥ 22 or K% > 21%
        is_hard_k = (opp_k_rank is not None and opp_k_rank <= 9) or \
                    (opp_k_pct is not None and opp_k_pct < 0.20)
        is_easy_k = (opp_k_rank is not None and opp_k_rank >= 22) or \
                    (opp_k_pct is not None and opp_k_pct > 0.21)

        if is_hard_k:
            severe = (opp_k_rank is not None and opp_k_rank <= 2) or \
                     (opp_k_pct is not None and opp_k_pct < 0.175)
            strong = (opp_k_rank is not None and opp_k_rank <= 5) or \
                     (opp_k_pct is not None and opp_k_pct < 0.19)
            adj = 5 if severe else (3 if strong else 2)  # mild tier added
            if is_under:
                score += adj  # hard to K = fewer Ks = good for Under
            else:
                score -= adj  # hard to K = fewer Ks = bad for Over
                if adj >= 3:
                    risk_flags.append("matchup")  # only the real risk tiers flag
                k_detail = (f"#{opp_k_rank}/30 hardest to K" if opp_k_rank else
                            f"{(opp_k_pct or 0)*100:.1f}% K rate")
                penalty_desc.append(
                    f"⚠️ **Matchup penalty −{adj}** — opponent {k_detail} (contact lineup)."
                )

        if is_easy_k:
            severe = (opp_k_rank is not None and opp_k_rank >= 29) or \
                     (opp_k_pct is not None and opp_k_pct > 0.26)
            strong = (opp_k_rank is not None and opp_k_rank >= 26) or \
                     (opp_k_pct is not None and opp_k_pct > 0.22)
            adj = 5 if severe else (3 if strong else 2)  # mild tier added
            if is_under:
                score -= adj  # K-prone = more Ks = bad for Under
                if adj >= 3:
                    risk_flags.append("matchup")  # only the real risk tiers flag
                k_detail = (f"#{opp_k_rank}/30 in K rate" if opp_k_rank else
                            f"{(opp_k_pct or 0)*100:.1f}% K rate")
                penalty_desc.append(
                    f"⚠️ **Matchup penalty −{adj}** — opponent {k_detail} (K-prone lineup)."
                )
            else:
                score += adj  # K-prone = more Ks = good for Over

    # Flag 2 — Short-term fade (L5 form collapsed)
    # Require actual L5 data — if l5 is empty (no games) we can't say form collapsed.
    # Graduated: ≤40% = real collapse (risk flag), ≤50% = mild cold, >50% = neutral
    if has_l5_data:
        if   eff_l5 <= 40:
            score -= 6
            risk_flags.append("form")
            penalty_desc.append(f"⚠️ **Form penalty −6** — L5 only {eff_l5:.0f}% (cold streak).")
        elif eff_l5 <= 50:
            score -= 3
            penalty_desc.append(f"📉 **Form dip −3** — L5 only {eff_l5:.0f}% (mild cold).")

    # ── Matchup-contradiction cap — "read the spot like a human" ─────────────
    # A big streak can carry a pick that the matchup clearly argues against
    # (e.g. an Under on a hitter who crushes this hand, facing a bad arm, in a
    # hitter's park). Count hard signals that oppose the chosen side; 2+ means
    # the label can't exceed Good no matter how hot the streak is.
    contra = 0
    contra_reasons = []
    if hand_ops_score <= -2:   # batter handles this pitcher's hand well (Under) / poorly (Over)
        contra += 1
        contra_reasons.append("handedness split")
    try:
        _era_c = float((pitcher or {}).get("era") or 0)
        if is_under and _era_c >= 4.50:
            contra += 1; contra_reasons.append(f"hittable arm ({_era_c:.2f} ERA)")
        elif (not is_under) and 0 < _era_c <= 3.00:
            contra += 1; contra_reasons.append(f"ace arm ({_era_c:.2f} ERA)")
    except (TypeError, ValueError):
        pass
    if is_under and park_factor >= 1.08:
        contra += 1; contra_reasons.append(f"hitter park ({park_factor:.2f}x)")
    elif (not is_under) and park_factor <= 0.92:
        contra += 1; contra_reasons.append(f"pitcher park ({park_factor:.2f}x)")
    # BvP with meaningful sample (≥8 AB) directly opposing the prop direction
    if bvp_ab >= 8:
        if (not is_under) and _bvp_scored_avg <= 0.150:
            contra += 1; contra_reasons.append(f"historically 0wned by this pitcher ({bvp_ab} AB, {bvp.get('avg','.000')} AVG)")
        elif is_under and _bvp_scored_avg >= 0.350:
            contra += 1; contra_reasons.append(f"batter crushes this pitcher ({bvp_ab} AB, {bvp.get('avg','.000')} AVG)")

    matchup_contradiction = contra >= 2
    if matchup_contradiction:
        risk_flags.append("matchup_contradiction")
        penalty_desc.append(
            f"⚠️ **Matchup contradiction** — {contra} signals oppose the "
            f"{'Under' if is_under else 'Over'} ({', '.join(contra_reasons)}); capped at Good.")

    # ── Hard cap: two+ risk flags OR a matchup contradiction → force "Good" ───
    force_cap = len(risk_flags) >= 2 or matchup_contradiction

    # ── Label resolution ──────────────────────────────────────────────────────
    def _resolve(s: int, capped: bool) -> dict:
        if s >= 10 and not capped:
            return {"label": "Elite",  "emoji": "💎", "color": 0x00D4FF,
                    "recommendation": "🟢 Play it. Full bet recommended — elite data alignment."}
        if s >= 6  and not capped:
            return {"label": "Strong", "emoji": "🔥", "color": 0x5865F2,
                    "recommendation": "✅ Play it. Real edge confirmed — full bet recommended."}
        if s >= 3  or  capped:
            return {"label": "Good",   "emoji": "✅", "color": 0x57F287,
                    "recommendation": "👍 Solid value — size down, multiple risk flags active." if capped
                                      else "👍 Solid positioning. High viability value play."}
        if s >= 0:
            return {"label": "Lean",  "emoji": "➡️", "color": 0xFEE75C,
                    "recommendation": "⚠️ Marginal edge. Proceed with caution — size down."}
        if s >= -10:
            return {"label": "Risky", "emoji": "⚠️", "color": 0xED4245,
                    "recommendation": "❌ Stay away. Risk penalties overwhelm historical edge."}
        return     {"label": "Fade",  "emoji": "🚫", "color": 0x2C2C2C,
                    "recommendation": "🚫 Hard fade. Do not play — multiple severe risk flags."}

    result = _resolve(score, force_cap)
    result["score"]          = score
    result["risk_flags"]     = risk_flags
    result["penalty_desc"]   = penalty_desc
    result["force_capped"]   = force_cap
    result["proj_edge"]      = round(proj_edge, 2)
    result["stability_tier"] = stability_tier
    result["lineup_spot"]    = lineup_spot
    result["proj_pa"]        = proj_pa
    result["damage_score"]   = damage_score
    result["discipline_score"] = discipline_score
    result["ump_score"]      = ump_score
    result["pitch_mix_score"] = pitch_mix_score
    result["hand_ops_score"] = hand_ops_score
    result["bullpen_score"]  = bullpen_score
    return result


def grade_pick_both(
    splits, line, opp_k_rank=None, opp_k_pct=None, pitcher=None, bvp=None,
    park_factor=1.0, weather=None, team_bvp=None, oaa=None, prop_type="",
    lineup_spot=None, statcast=None, team_h2h=None, arsenal=None,
    bat_vs_pitch=None, vs_hand_splits=None, learned_weight=None, umpire=None,
    is_home=None, opp_bullpen=None,
) -> dict:
    """
    Grade BOTH sides independently and return a comparison.
    The model verdict comes from data, not from user selection.
    Returns:
    {
        "selected_side": "over" | "under",
        "model_verdict": "over" | "under",
        "over_score": int,
        "under_score": int,
        "over_grade": dict,   # full grade_pick result for OVER
        "under_grade": dict,  # full grade_pick result for UNDER
        "disagreement": bool, # True when model disagrees with user selection
        "confidence": float,  # 0.0–1.0, how confident the model is in its verdict
    }
    """
    _kwargs = dict(
        splits=splits, line=line, opp_k_rank=opp_k_rank, opp_k_pct=opp_k_pct,
        pitcher=pitcher, bvp=bvp, park_factor=park_factor, weather=weather,
        team_bvp=team_bvp, oaa=oaa, prop_type=prop_type, lineup_spot=lineup_spot,
        statcast=statcast, team_h2h=team_h2h, arsenal=arsenal,
        bat_vs_pitch=bat_vs_pitch, vs_hand_splits=vs_hand_splits,
        learned_weight=learned_weight, umpire=umpire, is_home=is_home,
        opp_bullpen=opp_bullpen,
    )

    over_grade  = grade_pick(side="over",  **_kwargs)
    under_grade = grade_pick(side="under", **_kwargs)

    over_score  = over_grade["score"]
    under_score = under_grade["score"]

    # Model verdict: which side has the higher score.
    # Confidence is capped at 0.90 — no prop is ever a 100% certainty; the cap
    # keeps the number honest (a wide score gap means "strong lean", not "lock").
    if over_score >= under_score:
        model_verdict = "over"
        confidence = min(0.90, max(0.5, 0.5 + (over_score - under_score) / 24))
    else:
        model_verdict = "under"
        confidence = min(0.90, max(0.5, 0.5 + (under_score - over_score) / 24))

    return {
        "model_verdict": model_verdict,
        "over_score": over_score,
        "under_score": under_score,
        "over_grade": over_grade,
        "under_grade": under_grade,
        "confidence": round(confidence, 2),
    }


# ── 6. Discord embed builder ─────────────────────────────────────────────────

def build_analyze_embed(
    player_name:      str,
    team:             str,
    prop_type:        str,
    line:             float,
    splits:           dict,
    grade:            dict,
    matchup:          dict,
    bvp:              dict,
    side:             str        = "over",
    multi_prop_note:  str | None = None,
    pitcher_card:     dict | None = None,
    ev_pct:           float | None = None,
    book_name:        str | None = None,
    true_prob:        float | None = None,
    weather:          dict | None = None,
    team_bvp:         dict | None = None,
    oaa:              dict | None = None,
    arsenal:          list | None = None,
    bat_vs_pitch:     list | None = None,
    statcast:         dict | None = None,
    bullpen:          dict | None = None,
    umpire:           dict | None = None,
    batter_hand:      str         = "",
    park_factor:      float       = 1.0,
    lineup_spot:      int | None  = None,
    game_time:        str | None  = None,
    vs_hand_splits:   dict | None = None,
    team_h2h:         dict | None = None,   # from stats_mlb.get_vs_team_splits()
    side_comparison:  dict | None = None,   # from grade_pick_both() — model verdict vs user selection
) -> discord.Embed:
    """
    Six-layer premium analysis card:
      1. Header & Metrics Summary
      2. Historical Ceiling (WHY)
      3. Split Factor
      4. Matchup Dynamic
      5. Raw Summary (Recent + Matchup)
      6. Risk & Legend
    """
    market   = _MARKET_DISPLAY.get(prop_type, prop_type)
    emoji    = grade["emoji"]
    label    = grade["label"]
    score    = grade["score"]
    color    = grade["color"]

    # Determine which side the MODEL recommends (for hit rates and display).
    # When side_comparison is present, use the model's verdict for all stats.
    # The title still shows user's selection via `side`, but everything else
    # uses the model's recommended side.
    if side_comparison:
        model_side = side_comparison["model_verdict"]
    else:
        model_side = side
    is_under = model_side.lower() == "under"
    side_lbl = "Over" if model_side.lower() == "over" else "Under"
    # User's selected side (shown separately in disagreement banner)
    user_side = side.lower()
    user_side_lbl = "Over" if user_side == "over" else "Under"
    SIDE_UP  = side_lbl.upper()
    # Display line without trailing ".0" — 5.0 → "5", 4.5 → "4.5"
    line_str = f"{float(line):g}"

    # ── Unpack splits ─────────────────────────────────────────────────────────
    l5  = splits.get("l5")  or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}
    has_l20     = bool(l20.get("games", 0))
    l10_avg     = l10.get("avg", 0) or 0
    l10_rate_ov = l10.get("rate", 0) or 0          # raw over hit rate
    l5_rate_ov  = l5.get("rate",  0) or 0
    l20_rate_ov = l20.get("rate", 0) or 0
    season_avg  = splits.get("season_avg", 0) or 0
    gp          = splits.get("games_played", 0) or 0
    recent_games = splits.get("recent_games") or []

    # Effective hit rates (flip for Under)
    eff_l5  = (100 - l5_rate_ov)  if is_under else l5_rate_ov
    eff_l10 = (100 - l10_rate_ov) if is_under else l10_rate_ov
    eff_l20 = (100 - l20_rate_ov) if is_under else l20_rate_ov

    def _hfmt(d, under=False):
        """X/N (Y%)"""
        h = d.get("hits",  0)
        g = d.get("games", 0)
        if not g:
            return "—"
        if under:
            return f"{g - h}/{g} ({100 - (d.get('rate',0) or 0):.0f}%)"
        return f"{h}/{g} ({(d.get('rate',0) or 0):.0f}%)"

    # L10 avg vs line gap
    gap     = round(l10_avg - line, 2)
    gap_str = (f"+{gap}" if gap >= 0 else str(gap))

    # ── Matchup / spot ────────────────────────────────────────────────────────
    is_home      = matchup.get("is_home")
    spot_icon    = "🏠 Home" if is_home is True else ("✈️ Away" if is_home is False else "")
    pitcher_name = matchup.get("pitcher") or "TBD"
    opponent     = matchup.get("opponent") or ""

    # ── Pitcher card fields (optional — only present for K props) ─────────────
    pc           = pitcher_card or {}
    _pc_ss       = pc.get("season_stats") or {}  # K-cards store stats here
    pc_era       = pc.get("era",     _pc_ss.get("era",     "—"))
    pc_k9        = pc.get("k_per_9", _pc_ss.get("k_per_9", "—"))
    pc_whip      = pc.get("whip",    _pc_ss.get("whip",    "—"))
    pc_home_era  = pc.get("home_era")
    pc_away_era  = pc.get("away_era")
    pc_last5     = pc.get("last_5_starts") or []
    opp_k        = pc.get("opp_k") or {}
    opp_k_rank   = opp_k.get("rank")
    # k_pct is stored as a percentage (22.6) but display/scoring expects a decimal (0.226)
    _raw_k_pct   = opp_k.get("k_pct")
    opp_k_pct    = (_raw_k_pct / 100) if _raw_k_pct is not None else None

    # ── BvP ───────────────────────────────────────────────────────────────────
    bvp         = bvp or {}
    bvp_ab      = bvp.get("ab",   0)
    bvp_avg     = bvp.get("avg",  ".---")
    bvp_ops     = bvp.get("ops",  "")
    bvp_k       = bvp.get("k",    0)
    bvp_hr      = bvp.get("hr",   0)
    if bvp_ab >= 3:
        _bvp_parts = [f"**{bvp.get('hits',0)}/{bvp_ab}** ({bvp_avg} AVG"]
        if bvp_ops and bvp_ops != ".---":
            _bvp_parts[0] += f" · {bvp_ops} OPS"
        _bvp_parts[0] += ")"
        _bvp_extras = []
        if bvp_k:
            _bvp_extras.append(f"{bvp_k} K")
        if bvp_hr:
            _bvp_extras.append(f"{bvp_hr} HR")
        if _bvp_extras:
            _bvp_parts.append("· " + " · ".join(_bvp_extras))
        _bvp_parts.append(f"— {bvp.get('sample', 'small sample')}")

        # Plain-language verdict — tells new users if this matchup helps or hurts
        try:
            _bvp_f = float("0" + bvp_avg) if str(bvp_avg).startswith(".") else float(bvp_avg)
        except (ValueError, TypeError):
            _bvp_f = 0.0
        _p_first  = (player_name or "Batter").split()[0]
        _pit_last = (pitcher_name or "pitcher").split()[-1]
        # Sample-aware: only an 8+ AB history earns strong "destroys/dominates"
        # language. A 3–7 AB edge is a soft indicator, not a verdict.
        _big_bvp = bvp_ab >= 8
        if not is_under:
            if   _bvp_f >= 0.380: _bvp_verdict = (f"🔥 **{_p_first} owns {_pit_last}** — big boost for the Over." if _big_bvp
                                                  else f"✅ Small-sample edge — {_p_first} is {bvp.get('hits',0)}/{bvp_ab} vs {_pit_last}; a mild positive lean.")
            elif _bvp_f >= 0.300: _bvp_verdict = (f"✅ **{_p_first} hits {_pit_last} well** — leans Over." if _big_bvp
                                                  else f"➡️ Slight positive ({bvp.get('hits',0)}/{bvp_ab}) — too small to weight heavily.")
            elif _bvp_f >= 0.240: _bvp_verdict = "➡️ **Neutral matchup** — history doesn't strongly favor either side."
            elif _bvp_f >= 0.180: _bvp_verdict = (f"⚠️ **{_pit_last} has the edge on {_p_first}** — leans Under." if _big_bvp
                                                  else f"➡️ Slightly negative ({bvp.get('hits',0)}/{bvp_ab}) — small sample, minor signal.")
            else:                 _bvp_verdict = (f"🚫 **{_pit_last} dominates {_p_first}** — big boost for the Under." if _big_bvp
                                                  else f"⚠️ Cold in a small sample ({bvp.get('hits',0)}/{bvp_ab}) — minor negative.")
        else:
            if   _bvp_f >= 0.380: _bvp_verdict = (f"⚠️ **{_p_first} owns {_pit_last}** — this hurts the Under." if _big_bvp
                                                  else f"➡️ Small-sample risk — {_p_first} is {bvp.get('hits',0)}/{bvp_ab} vs {_pit_last}.")
            elif _bvp_f >= 0.300: _bvp_verdict = f"➡️ {_p_first} has hit {_pit_last} a bit — minor risk for the Under."
            elif _bvp_f >= 0.240: _bvp_verdict = "➡️ **Neutral matchup** — history doesn't strongly favor either side."
            elif _bvp_f >= 0.180: _bvp_verdict = (f"✅ **{_pit_last} has the edge on {_p_first}** — supports the Under." if _big_bvp
                                                  else f"➡️ Slight Under lean ({bvp.get('hits',0)}/{bvp_ab}) — small sample.")
            else:                 _bvp_verdict = (f"🔥 **{_pit_last} dominates {_p_first}** — big boost for the Under." if _big_bvp
                                                  else f"✅ Cold in a small sample ({bvp.get('hits',0)}/{bvp_ab}) — modest Under support.")

        _bvp_parts.append(f"\n{_bvp_verdict}")
        bvp_h2h_str = " ".join(_bvp_parts)
    else:
        bvp_h2h_str = "No prior history" if bvp_ab == 0 else f"{bvp_ab} AB — too small to grade"

    # ── EV / book — only shown when data is actually available ───────────────
    ev_str   = f"{ev_pct:+.1f}%" if ev_pct is not None else None
    book_str = book_name or None
    prob_str = f"{true_prob:.0f}%" if true_prob is not None else None

    # ── Trend label — side-aware so "heating up" means the REQUESTED side is hot ─
    if eff_l5 >= 80:
        trend_lbl = "🔥 Under streak building" if is_under else "🔥 heating up"
    elif eff_l5 <= 40:
        trend_lbl = "❄️ Under form fading"    if is_under else "📉 cooling off"
    else:
        trend_lbl = "➡️ steady"

    # ── Recent log string: "2 1 3 2 0" ──────────────────────────────────────
    recent_log = "  ".join(str(g["value"]) for g in recent_games) if recent_games else "—"

    # ── L5 advanced: compare L3 avg to season avg ────────────────────────────
    l3_vals = [g["value"] for g in recent_games[:3]]
    l3_avg  = round(sum(l3_vals) / len(l3_vals), 2) if l3_vals else None
    if l3_avg is not None and season_avg:
        l3_delta  = round(l3_avg - season_avg, 2)
        adv_trend = "spiking" if l3_delta > 0 else "dipping"
        adv_str   = (
            f"📉 L3 avg **{l3_avg}** vs **{season_avg}** season avg "
            f"({'+' if l3_delta >= 0 else ''}{l3_delta}) — {adv_trend} in recent sample."
        )
    else:
        adv_str = "—"

    # ── Home/away venue split ────────────────────────────────────────────────
    home_avg  = splits.get("home_avg")
    away_avg  = splits.get("away_avg")
    home_rate = splits.get("home_rate")   # % Over at home (last 20G)
    away_rate = splits.get("away_rate")   # % Over on the road (last 20G)
    home_games_ct = splits.get("home_games", 0) or 0
    away_games_ct = splits.get("away_games", 0) or 0

    def _rate_str(rate):
        return f" · {rate:.0f}% Over rate" if rate is not None else ""

    if home_avg is not None and away_avg is not None:
        h_str = f"**{home_avg}**{_rate_str(home_rate)} ({home_games_ct}G)"
        a_str = f"**{away_avg}**{_rate_str(away_rate)} ({away_games_ct}G)"
        if is_home is True:
            split_fav  = f"🏠 Home: {h_str} · ✈️ Road: {a_str}"
            split_note = ("Home splits favor the " + side_lbl
                          if (is_under and home_avg < away_avg) or (not is_under and home_avg > away_avg)
                          else "Away splits are actually stronger this season")
        elif is_home is False:
            split_fav  = f"✈️ Road: {a_str} · 🏠 Home: {h_str}"
            split_note = ("Road splits favor the " + side_lbl
                          if (is_under and away_avg < home_avg) or (not is_under and away_avg > home_avg)
                          else "Home splits are stronger — monitor carefully")
        else:
            split_fav  = f"🏠 Home: {h_str} · ✈️ Road: {a_str}"
            split_note = "Spot unknown — no game found for tonight."
    else:
        split_fav  = "Split data unavailable"
        split_note = ""

    # ── Risk flags ────────────────────────────────────────────────────────────
    risks = []
    if eff_l5 < 50:
        risks.append(f"📉 L5 only {eff_l5:.0f}% — recent form not confirming the {side_lbl}.")
    if has_l20 and eff_l20 < 55:
        risks.append(f"📊 L20 {eff_l20:.0f}% — long-run base rate is below 55%.")
    if is_under and l10_avg < line * 0.85:
        risks.append(f"📐 L10 avg {l10_avg} is well below the {line_str} line — Under math is solid but no upside cushion.")
    if not is_under and l10_avg < line:
        risks.append(f"📐 L10 avg {l10_avg} is below the {line_str} line — Over requires above-average output.")
    if bvp_ab >= 4:
        try:
            bvp_f = float(str(bvp_avg).lstrip(".").replace(".", "0.", 1)) if "." in str(bvp_avg) else float(bvp_avg)
        except (ValueError, TypeError):
            bvp_f = 0
        if not is_under and bvp_f < 0.200:
            risks.append(f"🆚 BvP avg {bvp_avg} over {bvp_ab} AB — struggles vs this arm historically.")
    if not risks:
        risks.append("No major red flags in available data.")
    if len(risks) < 2:
        risks.append("Sample size may be limited — treat with appropriate sizing.")

    # ── Unit sizing suggestion ────────────────────────────────────────────────
    if label == "Elite":   unit = "1.0u"
    elif label == "Strong": unit = "0.75u"
    elif label == "Good":   unit = "0.5u"
    else:                   unit = "0.25u"

    # ── Action line ───────────────────────────────────────────────────────────
    if label in ("Elite", "Strong", "Good"):
        action = f"✦ Play it — {unit}"
    elif label == "Lean":
        action = f"↘ Lean only — size down ({unit})"
    else:
        action = "✖ Fade — stay away"

    # ══════════════════════════════════════════════════════════════════════════
    # BUILD EMBED
    # ══════════════════════════════════════════════════════════════════════════
    embed = discord.Embed(
        title=f"{player_name} — {user_side_lbl} {line_str} {market}",
        color=color,
    )

    if multi_prop_note:
        embed.add_field(name="— notice", value=multi_prop_note, inline=False)

    # ── LAYER 1: Header & Metrics Summary ─────────────────────────────────────
    team_tag = f" ({team})" if team else ""
    status_parts = [f"{emoji} **{label}** (Score {score})"]
    if prob_str:
        status_parts.append(f"est. **{eff_l10:.0f}%** vs {prob_str} market")
    else:
        status_parts.append(f"est. **{eff_l10:.0f}%** hit rate")
    if ev_str:
        status_parts.append(f"**{ev_str}** value")
    if book_str:
        status_parts.append(book_str)
    if spot_icon:
        status_parts.append(spot_icon)
    if game_time:
        status_parts.append(f"🕐 {game_time}")

    _status_line = " · ".join(status_parts)
    embed.add_field(
        name=f"{player_name}{team_tag} — {side_lbl} {line_str} {market}",
        value=f"{_status_line}\n{action}",
        inline=False,
    )

    # ── LAYER 2: Historical Ceiling ────────────────────────────────────────────
    l5_h  = l5.get("hits", 0)
    l5_g  = l5.get("games", 0)
    l10_h = l10.get("hits", 0)
    l10_g = l10.get("games", 0)
    l20_h = l20.get("hits", 0)
    l20_g = l20.get("games", 0)

    l20_clause = ""
    if has_l20:
        if is_under:
            l20_clause = f", and **{l20_g - l20_h}/{l20_g}** over his last 20 ({eff_l20:.0f}%)"
        else:
            l20_clause = f", and **{l20_h}/{l20_g}** over his last 20 ({eff_l20:.0f}%)"

    # ── Core reason (K props only) — one-line projection driver ─────────────
    core_reason = ""
    if prop_type == "strikeouts":
        _pc_ss_cr = (pitcher_card or {}).get("season_stats") or {}
        _k9_cr    = (pitcher_card or {}).get("k_per_9") or _pc_ss_cr.get("k_per_9", "")
        try:
            _k9_f_cr = float(str(_k9_cr))
            _kf_cr   = (opp_k_pct or 0.22) / 0.22
            _ip_cr   = float(str((pitcher_card or {}).get("innings_pitched", "5.4") or "5.4").split(".")[0]) + 0.4
            _proj_cr = round(_k9_f_cr * _kf_cr * (_ip_cr / 9), 1)
            _avg_gap = round(float(l10_avg) - float(line), 1) if l10_avg else None
            _dir     = "UNDER" if _proj_cr < float(line) else "OVER"
            _gap_str = (f" — **{abs(_avg_gap)} below**" if _avg_gap is not None and _avg_gap < 0 else
                        f" — **{_avg_gap} above**"       if _avg_gap is not None and _avg_gap > 0 else "")
            opp_ctx  = (f", even accounting for the {'K-prone' if (opp_k_pct or 0) >= 0.22 else 'contact-heavy'} "
                        f"{opponent} lineup" if opponent else "")
            core_reason = (
                f"🔍 **Core projection: {_dir} {line_str}** — "
                f"model projects **{_proj_cr} Ks** ({_k9_cr} K/9 × {_kf_cr:.3f} factor × {_ip_cr:.1f} IP){opp_ctx}. "
                f"L10 avg is **{l10_avg}**{_gap_str} the line."
            )
        except (ValueError, TypeError):
            pass

    gp = splits.get("games_played", 0) or 0
    if l10_g == 0 and l5_g >= 3:
        # Not enough starts for L10 — fall back to L5
        if is_under:
            ceiling_narrative = (
                f"{player_name} has gone **{side_lbl} {line_str}** in "
                f"**{l5_g - l5_h}/{l5_g}** of his last {l5_g} games ({eff_l5:.0f}%)"
                f" — only {gp} starts this season, no L10/L20 data."
            )
        else:
            ceiling_narrative = (
                f"{player_name} has hit **{side_lbl} {line_str}** in "
                f"**{l5_h}/{l5_g}** of his last {l5_g} games ({eff_l5:.0f}%)"
                f" — only {gp} starts this season, no L10/L20 data."
            )
    elif l10_g == 0:
        ceiling_narrative = f"{player_name} has only {gp} starts this season — limited sample for hit-rate analysis."
    elif is_under:
        ceiling_narrative = (
            f"{player_name} has gone **{side_lbl} {line_str}** in "
            f"**{l10_g - l10_h}/{l10_g}** of his last 10 games ({eff_l10:.0f}%)"
            f"{l20_clause}."
        )
    else:
        ceiling_narrative = (
            f"{player_name} has hit **{side_lbl} {line_str}** in "
            f"**{l10_h}/{l10_g}** of his last 10 games ({eff_l10:.0f}%)"
            f"{l20_clause}."
        )

    # Active streak — L5 streak: positive=over, negative=under
    raw_streak   = l5.get("streak", 0) or 0
    # For Under props, going Under IS a hit — invert sign
    eff_streak   = -raw_streak if is_under else raw_streak
    streak_line  = ""
    if eff_streak >= 3:
        side_lbl = "Under" if is_under else "Over"
        streak_line = f"🔥 **{eff_streak}-game {side_lbl} streak** — prop has hit {eff_streak} straight."
    elif eff_streak <= -3:
        miss_lbl = "Over" if is_under else "Under"
        streak_line = f"❄️ **{abs(eff_streak)}-game {miss_lbl} streak** — prop hasn't hit recently, form concern."

    # Trending narrative (L5 vs L20) — side-aware labels
    trend_line = ""
    if has_l20 and l20_g >= 10:
        l5_pct  = eff_l5
        l20_pct = eff_l20
        _side_word = "Under" if is_under else "Over"
        if l5_pct - l20_pct >= 20:
            if is_under:
                trend_line = f"📈 **{_side_word} trending up** — {l5_pct:.0f}% Under L5 vs {l20_pct:.0f}% L20. Under form building."
            else:
                trend_line = f"📈 **Trending up** — {l5_pct:.0f}% L5 vs {l20_pct:.0f}% L20. Hot right now."
        elif l20_pct - l5_pct >= 20:
            if is_under:
                trend_line = f"📉 **{_side_word} trending down** — {l5_pct:.0f}% Under L5 vs {l20_pct:.0f}% L20. Under form fading."
            else:
                trend_line = f"📉 **Trending down** — {l5_pct:.0f}% L5 vs {l20_pct:.0f}% L20. Cooling off."

    # Pitch arsenal matchup
    arsenal     = arsenal or []
    bat_vs_pitch = bat_vs_pitch or []
    arsenal_lines = []
    if arsenal and bat_vs_pitch:
        # Cross-reference: for each top pitch in arsenal, find batter's stats vs it
        bvp_map = {r["pitch_type"]: r for r in bat_vs_pitch}
        for pitch in arsenal[:3]:  # top 3 pitches by usage
            pt    = pitch["pitch_type"]
            pname = pitch["pitch_name"]
            pct   = pitch["pct"]
            if pt in bvp_map:
                bvp_r = bvp_map[pt]
                pa    = bvp_r["pa"]
                avg   = bvp_r["avg"]
                ops   = bvp_r["ops"]
                try:
                    avg_f = float("0" + avg) if avg.startswith(".") else float(avg)
                    ops_f = float("0" + ops) if ops.startswith(".") else float(ops)
                    if avg_f >= 0.300 or ops_f >= 0.850:
                        icon = "🎯"
                    elif avg_f <= 0.180:
                        icon = "⚠️"
                    else:
                        icon = "📊"
                    arsenal_lines.append(
                        f"{icon} **vs {pname}** ({pct:.0f}% usage) — {avg} AVG / {ops} OPS over {pa} PA"
                    )
                except (ValueError, TypeError):
                    pass
    elif arsenal:
        # At least show what the pitcher throws
        top2 = [f"**{p['pitch_name']}** ({p['pct']:.0f}%)" for p in arsenal[:2]]
        if top2:
            arsenal_lines.append(f"🎯 Primary pitches: {' · '.join(top2)}")

    # Statcast quality of contact
    sc_line = ""
    if statcast:
        brl   = statcast.get("barrel_pct", 0) or 0
        hh    = statcast.get("hard_hit_pct", 0) or 0
        xslg  = statcast.get("xslg", "")
        xwoba = statcast.get("xwoba", "")
        sc_parts = []
        if brl:   sc_parts.append(f"Barrel **{brl:.1f}%**")
        if hh:    sc_parts.append(f"HH **{hh:.1f}%**")
        if xslg:  sc_parts.append(f"xSLG **{xslg}**")
        if xwoba: sc_parts.append(f"xwOBA **{xwoba}**")
        if sc_parts:
            # Label off the BEST of all four metrics — xSLG/xwOBA matter as much
            # as barrel/HH (an elite .480 xwOBA shouldn't read "avg" just because
            # the barrel field is missing).
            def _f(v):
                try:
                    s = str(v).strip()
                    return float("0" + s) if s.startswith(".") else float(s)
                except (TypeError, ValueError):
                    return 0.0
            _xslg_f  = _f(xslg)
            _xwoba_f = _f(xwoba)
            _elite   = brl >= 10 or hh >= 45 or _xwoba_f >= 0.380 or _xslg_f >= 0.500
            _above   = brl >= 6  or hh >= 35 or _xwoba_f >= 0.340 or _xslg_f >= 0.430
            if _elite:
                sc_icon, sc_note = "💥", "elite contact quality"
            elif _above:
                sc_icon, sc_note = "✅", "above-avg contact"
            else:
                sc_icon, sc_note = "📊", "avg contact quality"
            sc_line = f"{sc_icon} {' · '.join(sc_parts)} — {sc_note}"

    # Plate discipline: Chase%, Zone-contact%, Whiff%
    pd_line = ""
    if statcast:
        chase = statcast.get("chase_pct", 0) or 0
        zcon  = statcast.get("zone_contact_pct", 0) or 0
        whiff = statcast.get("whiff_pct", 0) or 0
        pd_parts = []
        if chase:  pd_parts.append(f"Chase **{chase:.1f}%**")
        if zcon:   pd_parts.append(f"Z-Contact **{zcon:.1f}%**")
        if whiff:  pd_parts.append(f"Whiff **{whiff:.1f}%**")
        if pd_parts:
            flags = []
            if chase >= 32 and not is_under:
                flags.append("⚠️ high chase — breaking ball risk")
            elif chase <= 22 and not is_under:
                flags.append("✅ disciplined eye")
            if zcon >= 86:
                flags.append("💥 elite bat-to-ball in zone")
            elif zcon and zcon <= 76:
                flags.append("⚠️ weak in-zone contact")
            flag_str = " · ".join(flags)
            pd_line = f"🎯 {' · '.join(pd_parts)}" + (f" — {flag_str}" if flag_str else "")

    why_parts = [
        core_reason,       # K-prop projection driver (empty string for non-K props)
        ceiling_narrative,
        sc_line,
        pd_line,
        f"📉 L5: **{_hfmt(l5, is_under)}** — {trend_lbl}.",
        adv_str,
        streak_line,
        trend_line,
    ] + arsenal_lines

    _why_text = "\n".join(filter(None, why_parts))
    if len(_why_text) > 1024:
        _why_text = _why_text[:1021] + "..."
    embed.add_field(
        name="— why it hits",
        value=_why_text,
        inline=False,
    )

    # ── LAYER 3: Split Factor ──────────────────────────────────────────────────
    home_games = splits.get("home_games", 0) or 0
    away_games = splits.get("away_games", 0) or home_games  # fallback
    vol_avg    = round((home_avg or 0 + away_avg or 0) / 2, 2) if (home_avg and away_avg) else season_avg
    vol_label  = "heavy workload" if vol_avg >= line * 1.3 else "moderate workload" if vol_avg >= line * 0.9 else "light workload"

    venue_tag = ("🏠 **Tonight: at home**" if is_home is True
                 else "✈️ **Tonight: on the road**" if is_home is False
                 else None)

    split_body = "\n".join(filter(None, [
        venue_tag,
        f"📍 Venue split — {split_fav}",
        split_note,
        f"➡️ Volume: Season avg **{season_avg}** over **{gp} GP** — {vol_label}.",
    ]))
    if len(split_body) > 1024:
        split_body = split_body[:1021] + "..."
    embed.add_field(name="— split factor", value=split_body, inline=False)

    # ── LAYER 4: Matchup Dynamic ───────────────────────────────────────────────
    matchup_lines = []
    if opponent:
        if opp_k_rank and opp_k_pct:
            # K-prone opp (high K%) = more Ks for pitcher = favors OVER / hurts UNDER
            opp_is_k_prone = opp_k_pct >= 0.22
            if (not is_under and opp_is_k_prone) or (is_under and not opp_is_k_prone):
                k_verdict = f"favors the {SIDE_UP}"
                k_icon    = "🟢"
            else:
                k_verdict = f"works against the {SIDE_UP}"
                k_icon    = "🔴"
            matchup_lines.append(
                f"{k_icon} **{opponent}** ranks #{opp_k_rank}/30 in K rate "
                f"({opp_k_pct*100:.1f}%) — **{k_verdict}**."
            )
        else:
            matchup_lines.append(f"🆚 **{opponent}**{(' — vs ' + pitcher_name) if pitcher_name else ''}")

    _proj_k_for_audit = None  # lifted so auditor can access it after matchup block
    # Default so later blocks (e.g. handedness split) never hit an UnboundLocalError
    # when there's no confirmed pitcher and the matchup block below is skipped.
    pc_hand = (pc or {}).get("hand", "?")
    if pc and (pitcher_name or prop_type == "strikeouts"):
        pc_hr9  = pc.get("hr_per_9", "—")
        pc_fip  = pc.get("fip",      "—")
        pc_hand = pc.get("hand",     "?")

        if prop_type == "strikeouts":
            # ── K-prop: polarity-aware signals ───────────────────────────────────
            k9_val = pc_k9
            proj_k = None
            avg_ip = None
            try:
                k9_f    = float(str(k9_val))
                k_factor= (opp_k_pct or 0.22) / 0.22
                ip_est  = float(str(pc.get("innings_pitched", "5.4") or "5.4").split(".")[0]) + 0.4
                proj_k  = round(k9_f * k_factor * (ip_est / 9), 1)
                _proj_k_for_audit = proj_k
                avg_ip  = ip_est
                proj_side = "OVER" if proj_k > line else "UNDER"
                # Polarity: projection above line helps Over, below helps Under
                proj_helps = (proj_side == "OVER" and not is_under) or (proj_side == "UNDER" and is_under)
                proj_icon = "🟢" if proj_helps else "🔴"
                matchup_lines.append(
                    f"{proj_icon} Adjusted projection: **{proj_k} Ks** "
                    f"({k9_val} K/9 × {k_factor:.3f} K-factor × {ip_est:.1f} IP) "
                    f"→ model projects **{proj_side}** the {line_str} line "
                    f"({'✅ supports' if proj_helps else '⚠️ risks'} the {SIDE_UP})."
                )
            except (ValueError, TypeError):
                matchup_lines.append(
                    f"🎯 Facing **{pitcher_name}** ({pc_era} ERA · {pc_k9} K/9 · {pc_whip} WHIP)"
                )
        else:
            # Hitting prop: show pitcher vulnerability stats
            vuln_parts = [f"{pc_era} ERA", f"{pc_hr9} HR/9"]
            if pc_fip and pc_fip != "—":
                vuln_parts.append(f"{pc_fip} FIP")
            matchup_lines.append(
                f"🎯 Facing **{pitcher_name}** ({'L' if pc_hand == 'L' else 'R'}HP) — "
                + " · ".join(vuln_parts)
            )
            # BvP summary inline — show whenever there's any history
            if bvp_ab >= 3:
                matchup_lines.append(f"🆚 **BvP vs {pitcher_name}**: {bvp_h2h_str}")
            elif bvp_ab > 0:
                matchup_lines.append(f"🆚 **BvP vs {pitcher_name}**: {bvp_ab} AB — too few PA to grade")
        # TTO — times through the order from last 5 starts avg IP
        def _ips(ip_str):
            try:
                p = str(ip_str).split(".")
                return int(p[0]) + (int(p[1]) if len(p) > 1 else 0) / 3
            except Exception:
                return 0.0
        if pc_last5:
            ips_vals = [_ips(s.get("ip", "0")) for s in pc_last5 if s.get("ip")]
            if ips_vals:
                ip_avg_l5 = sum(ips_vals) / len(ips_vals)
                if prop_type == "strikeouts":
                    # Polarity: short leash = fewer Ks = ✅ Under / ⚠️ Over
                    if ip_avg_l5 < 4.5:
                        leash_icon = "🟢" if is_under else "🔴"
                        leash_note = "caps K ceiling" if is_under else "limits K accumulation"
                        matchup_lines.append(
                            f"{leash_icon} **Short leash** — {ip_avg_l5:.1f} IP avg → exits early "
                            f"({'✅ supports' if is_under else '⚠️ risks'} the {SIDE_UP}: fewer innings = fewer Ks)"
                        )
                    elif ip_avg_l5 >= 6.0:
                        deep_icon = "🔴" if is_under else "🟢"
                        matchup_lines.append(
                            f"{deep_icon} **Deep outings** — {ip_avg_l5:.1f} IP avg (3× through lineup) "
                            f"({'⚠️ risks' if is_under else '✅ supports'} the {SIDE_UP}: max K opportunity)"
                        )
                    else:
                        matchup_lines.append(f"➡️ **Moderate workload** — {ip_avg_l5:.1f} IP avg (2-3 looks at lineup)")
                else:
                    if ip_avg_l5 < 4.5:
                        matchup_lines.append(f"⏱️ **Short leash** — {ip_avg_l5:.1f} IP avg → bullpen by 5th inning")
                    elif ip_avg_l5 >= 6.0:
                        matchup_lines.append(f"🔁 **3 looks** at starter — {ip_avg_l5:.1f} IP avg (3× through order)")
                    else:
                        matchup_lines.append(f"🔁 **2-3 PA** vs starter — {ip_avg_l5:.1f} IP avg")

        # Full platoon — skip for pitcher K props (batter_hand = pitcher's own bat side, irrelevant)
        ph = pc.get("hand", "") if pc else ""
        if prop_type != "strikeouts" and batter_hand and ph and ph != "?":
            throws    = "left-handed" if ph == "L" else "right-handed"
            # vs_hand_splits is now {"L": {avg,ops,pa}, "R": {avg,ops,pa}}
            _vs_ph    = (vs_hand_splits or {}).get(ph, {})
            vs_avg    = _vs_ph.get("avg", "---")
            vs_ops    = _vs_ph.get("ops", "---")
            vs_pa     = _vs_ph.get("pa", 0) or 0
            _first    = player_name.split()[0]
            is_fav    = (batter_hand == "R" and ph == "L") or (batter_hand == "L" and ph == "R")

            if batter_hand == "S":
                _ctx = f" · {vs_avg} AVG vs {ph}HP" if vs_avg not in ("---", "") and vs_pa >= 20 else ""
                matchup_lines.append(f"↔️ **Handedness** — Switch hitter vs {throws} pitcher{_ctx}")
            elif vs_pa >= 20 and vs_avg not in ("---", ""):
                try:
                    _avg_f = float(vs_avg)
                    _ops_f = float(vs_ops) if vs_ops not in ("---", ".---", "") else None
                    _good  = _avg_f >= 0.280 or (_ops_f and _ops_f >= 0.800)
                    _poor  = _avg_f <= 0.220 or (_ops_f and _ops_f <= 0.650)
                    if is_under:
                        _icon = "🟢" if _poor else ("🔴" if _good else "🟡")
                    else:
                        _icon = "🟢" if _good else ("🔴" if _poor else "🟡")
                    _stats = f"**{vs_avg}** AVG"
                    if _ops_f:
                        _stats += f" · **{vs_ops}** OPS"
                    _stats += f" ({vs_pa} PA)"

                    # Pull opposite-hand split from the new {"L":..,"R":..} format
                    _opp_ph   = "L" if ph == "R" else "R"
                    _opp_hand = "LHP" if _opp_ph == "L" else "RHP"
                    _opp_data = (vs_hand_splits or {}).get(_opp_ph, {})
                    _opp_avg  = _opp_data.get("avg", "---")
                    _opp_ops  = _opp_data.get("ops", "---")
                    _opp_pa   = _opp_data.get("pa", 0) or 0

                    # Directional comparison — AVG is primary signal for "struggles vs X" label
                    _opp_avg_f  = None
                    _opp_ops_f  = None
                    _dir_worse  = False   # clearly worse vs pitcher's hand than opposite
                    _dir_better = False   # clearly better vs pitcher's hand than opposite
                    if _opp_avg not in ("---", "") and _opp_pa >= 20:
                        try:
                            _opp_avg_f = float(_opp_avg)
                            # Only flag directional if gap >= 0.020 AVG (noise floor)
                            if _avg_f < _opp_avg_f - 0.020:
                                _dir_worse  = True
                            elif _avg_f > _opp_avg_f + 0.020:
                                _dir_better = True
                            if _opp_ops not in ("---", ".---", "") and _ops_f:
                                _opp_ops_f = float(_opp_ops)
                        except (ValueError, TypeError):
                            pass

                    # Comparison line shown when opposite-split data exists
                    _comp = ""
                    if _opp_avg_f is not None:
                        _comp_str = f"**{_opp_avg}** AVG"
                        if _opp_ops_f is not None:
                            _comp_str += f" / **{_opp_ops}** OPS"
                        _comp = f" vs {_opp_hand}: {_comp_str} ({_opp_pa} PA)."

                    # Outcome phrasing:
                    # "struggles vs X-handed" only fires when he's directionally WORSE vs this hand.
                    # If he performs the same or better vs this hand (even if still poor in absolute
                    # terms), use the neutral "struggling regardless of handedness" phrasing.
                    if _good:
                        if _dir_better:
                            _outcome = f"{'a risk for' if is_under else 'a boost for'} the {SIDE_UP} — {_first} sees the ball well from {ph}-handed pitchers"
                        else:
                            _outcome = f"{'a risk' if is_under else 'a boost'} — {_first} is producing well regardless of handedness"
                    elif _poor:
                        if _dir_worse:
                            _outcome = f"{'a boost for' if is_under else 'a risk for'} the {SIDE_UP} — {_first} struggles against {ph}-handed pitching"
                        elif _dir_better:
                            _outcome = f"{'a mild boost for' if is_under else 'a slight concern for'} the {SIDE_UP} — {_first}'s output is below average here but he actually hits {ph}HP better than {_opp_hand}"
                        else:
                            _outcome = f"{'a boost' if is_under else 'a risk'} — {_first} is struggling to make consistent contact regardless of handedness"
                    else:
                        if _dir_worse:
                            _outcome = f"{'a mild boost for' if is_under else 'a mild risk for'} the {SIDE_UP} — {_first} performs slightly better against {_opp_hand}"
                        elif _dir_better:
                            _outcome = f"{'a mild risk for' if is_under else 'a mild boost for'} the {SIDE_UP} — {_first} performs slightly better against {ph}-handed pitchers"
                        else:
                            _outcome = f"a neutral factor — {_first} is about average against {ph}-handed pitchers"

                    matchup_lines.append(
                        f"{_icon} **Handedness** — This pitcher throws {throws}. "
                        f"{_first} hits {ph}HP at {_stats}.{_comp} This is {_outcome}."
                    )
                except (ValueError, TypeError):
                    _fb = "✅ Favorable platoon" if is_fav else "⚠️ Same-side matchup"
                    matchup_lines.append(f"{_fb} — {batter_hand}-handed batter vs {throws} pitcher")
            else:
                # Not enough vs-hand data — fall back to platoon label only
                if is_fav:
                    matchup_lines.append(f"✅ **Handedness** — Favorable platoon — {batter_hand}-handed batter vs {throws} pitcher")
                else:
                    matchup_lines.append(f"🟡 **Handedness** — Same-side matchup — {batter_hand}-handed batter vs {throws} pitcher (limited data)")

        matchup_lines.append(f"🏟️ {spot_icon}")

    # EPA — lineup spot and projected plate appearances
    _pa_est = {1: 4.5, 2: 4.4, 3: 4.2, 4: 4.1, 5: 3.9, 6: 3.8, 7: 3.7, 8: 3.6, 9: 3.5}
    if lineup_spot is not None:
        pa_proj  = _pa_est.get(lineup_spot, 4.0)
        spot_ord = {1:"1st",2:"2nd",3:"3rd",4:"4th",5:"5th",
                    6:"6th",7:"7th",8:"8th",9:"9th"}.get(lineup_spot, f"#{lineup_spot}")
        if lineup_spot <= 2:
            pa_icon = "🔝"
            pa_note = "max PA exposure — top-of-order run producer"
        elif lineup_spot <= 5:
            pa_icon = "⬆️"
            pa_note = "heart of order — above-avg opportunities"
        elif lineup_spot >= 8:
            pa_icon = "⬇️"
            pa_note = "bottom of order — fewer plate appearances"
        else:
            pa_icon = "➡️"
            pa_note = "middle order"
        matchup_lines.append(
            f"{pa_icon} Batting **{spot_ord}** — ~**{pa_proj}** projected PA tonight · {pa_note}"
        )

    elif pitcher_name:
        matchup_lines.append(f"🎯 Facing **{pitcher_name}** · {spot_icon}")

    if not matchup_lines:
        matchup_lines.append("No game found for tonight — check back closer to first pitch.")

    _matchup_text = "\n".join(matchup_lines)
    if len(_matchup_text) > 1024:
        _matchup_text = _matchup_text[:1021] + "..."
    embed.add_field(name="— matchup", value=_matchup_text, inline=False)

    # ── LAYER 5a: Verdict ─────────────────────────────────────────────────────
    pe            = grade.get("proj_edge", 0) or 0
    stab_tier     = grade.get("stability_tier", "")
    dmg           = grade.get("damage_score", 0) or 0
    pe_str        = (f"+{pe:.2f}" if pe > 0 else f"{pe:.2f}") if pe != 0 else None
    pe_icon       = "📈" if pe > 0 else ("📉" if pe < 0 else "")
    stab_icons    = {"HIGH": "🟢 High", "MEDIUM": "🟡 Medium",
                     "LOW": "🔴 Low", "VOLATILE": "⚡ Volatile"}
    stab_label    = stab_icons.get(stab_tier, "")
    dmg_labels    = {0: "", 1: "📊 Medium", 2: "💥 High", 3: "💥 High",
                     4: "🔥 Elite", 5: "🔥 Elite", 6: "🔥 Elite"}
    dmg_label     = dmg_labels.get(min(dmg, 6), "🔥 Elite")

    # Quick-glance stats line
    _eff_l10_str = f"{eff_l10:.0f}%" if l10.get("games", 0) else "—"
    quick_stats = f"L10 hit rate: **{_eff_l10_str}** · L10 avg: **{l10_avg}** vs **{line_str}** line ({gap_str})"
    if stab_label:
        quick_stats += f" · Stability: **{stab_label}**"
    if pe_str:
        quick_stats += f"\n{pe_icon} Projection edge: **{pe_str}** vs line"
    if dmg >= 1 and dmg_label:
        quick_stats += f" · Damage: **{dmg_label}**"
    if ev_str:
        quick_stats += f" · EV: **{ev_str}**"

    # ── Full narrative paragraph ──────────────────────────────────────────────
    _game_unit = "starts" if prop_type == "strikeouts" else "games"
    _first     = player_name.split()[0]
    para_parts = []

    def _safe_lt(val, thresh) -> bool:
        """True if val (str/num) parses to a float < thresh; False on bad data."""
        try:
            return float(val) < thresh
        except (TypeError, ValueError):
            return False

    # 0. Model conflict detection — flag when the data disagrees with requested side
    _conflict_note = None
    try:
        if prop_type == "strikeouts":
            _k9_c  = float(str(pc_k9))
            _kf_c  = (opp_k_pct or 0.22) / 0.22
            _ip_c  = float(str((pc or {}).get("innings_pitched", "5.4") or "5.4").split(".")[0]) + 0.4
            _proj_c = round(_k9_c * _kf_c * (_ip_c / 9), 1)
            _proj_c_side = "Over" if _proj_c > float(line) else "Under"
            _req_side    = "Under" if is_under else "Over"
            if _proj_c_side != _req_side:
                _conflict_note = (
                    f"⚠️ **MODEL CONFLICT** — You requested the **{SIDE_UP} {line_str}** but the model "
                    f"projects **{_proj_c} Ks**, which leans **{_proj_c_side}** the line. "
                    f"Everything below evaluates your requested side — read the signals carefully."
                )
        else:
            _l10_avg_c = float(l10_avg) if l10_avg and l10_avg != "—" else None
            _line_c    = float(line)
            if _l10_avg_c is not None:
                _data_leans_over = _l10_avg_c > _line_c
                _req_leans_over  = not is_under
                if _data_leans_over != _req_leans_over:
                    _natural = "Over" if _data_leans_over else "Under"
                    _conflict_note = (
                        f"⚠️ **MODEL CONFLICT** — You requested the **{SIDE_UP} {line_str}** but the L10 avg "
                        f"(**{l10_avg}**) sits {'above' if _data_leans_over else 'below'} the line, "
                        f"pointing **{_natural}**. The card below evaluates your requested side."
                    )
    except (ValueError, TypeError):
        pass

    if _conflict_note:
        para_parts.append(_conflict_note)

    # 1. Form anchor — the most important sentence
    if l10.get("games", 0):
        l10_count = (l10_h if not is_under else (l10_g - l10_h))
        try:
            _gap_val  = round(float(l10_avg) - float(line), 2)
            _gap_dir  = "below" if _gap_val < 0 else "above"
            _gap_abs  = abs(_gap_val)
            para_parts.append(
                f"**{_first}** has gone **{side_lbl} {line_str}** in **{l10_count}/{l10_g}** "
                f"of his last 10 {_game_unit} ({eff_l10:.0f}%), averaging **{l10_avg} {market}** "
                f"per game — **{_gap_abs} {_gap_dir}** tonight's {line_str} line."
            )
        except (ValueError, TypeError):
            para_parts.append(
                f"**{_first}** has gone **{side_lbl} {line_str}** in **{l10_count}/{l10_g}** "
                f"of his last 10 {_game_unit} ({eff_l10:.0f}%)."
            )

    # 2. Trend / streak context
    _raw_streak = l5.get("streak", 0) or 0
    _eff_streak = -_raw_streak if is_under else _raw_streak
    if _eff_streak >= 3:
        _side_lbl = "Under" if is_under else "Over"
        para_parts.append(f"He's hit the **{_side_lbl}** in **{_eff_streak} straight games** — strong prop momentum.")
    elif _eff_streak <= -3:
        _miss_lbl = "Over" if is_under else "Under"
        para_parts.append(f"He's gone **{_miss_lbl}** in his last **{abs(_eff_streak)} straight** — prop momentum is against this side.")
    elif eff_l5 >= 80:
        para_parts.append(f"His recent L5 form ({eff_l5:.0f}%) is strong and confirms the trend.")
    elif eff_l5 <= 40:
        para_parts.append(f"His L5 ({eff_l5:.0f}%) has cooled significantly — the recent trend is a risk.")

    # 3. Prop-type-specific context
    if prop_type == "strikeouts":
        # Projection
        _proj_para = None
        try:
            _k9_p  = float(str(pc_k9))
            _kf_p  = (opp_k_pct or 0.22) / 0.22
            _ip_p  = float(str(pc.get("innings_pitched", "5.4") or "5.4").split(".")[0]) + 0.4
            _proj_para = round(_k9_p * _kf_p * (_ip_p / 9), 1)
            _proj_vs   = "below" if _proj_para < float(line) else "above"
            para_parts.append(
                f"The model projection lands at **{_proj_para} Ks** "
                f"({pc_k9} K/9 × {_kf_p:.3f} matchup factor × {_ip_p:.1f} IP) — "
                f"**{abs(round(_proj_para - float(line), 1))} {_proj_vs}** the {line_str} line."
            )
        except (ValueError, TypeError):
            pass

        # Opp K rate — explain its role clearly
        if opp_k_pct and opp_k_rank and opponent:
            opp_k_prone = opp_k_pct >= 0.22
            if is_under and opp_k_prone:
                para_parts.append(
                    f"The key risk here: **{opponent}** is one of the more strikeout-prone lineups in baseball "
                    f"(#{opp_k_rank}/30, {opp_k_pct*100:.1f}% K rate), which creates real ceiling pressure "
                    f"against this Under. However, even with that K-prone factor applied, the projection "
                    f"({'and L10 avg ' if l10_avg else ''}still {'lands' if _proj_para else 'points'} below {line_str}."
                )
            elif is_under and not opp_k_prone:
                para_parts.append(
                    f"**{opponent}** is a contact-heavy lineup (#{opp_k_rank}/30, {opp_k_pct*100:.1f}% K rate) — "
                    f"that limits the pitcher's strikeout ceiling — works in the Under's favor."
                )
            elif not is_under and opp_k_prone:
                para_parts.append(
                    f"**{opponent}** is one of the more K-prone lineups in baseball "
                    f"(#{opp_k_rank}/30, {opp_k_pct*100:.1f}%) — that works in the Over's favor."
                )
            elif not is_under and not opp_k_prone:
                para_parts.append(
                    f"**{opponent}** ranks #{opp_k_rank}/30 in K rate ({opp_k_pct*100:.1f}%) — "
                    f"a contact-heavy lineup that works against the Over, limiting the pitcher's K ceiling "
                    f"even when K/9 numbers look strong."
                )

        # Leash / workload
        if pc_last5:
            try:
                def __ips(ip_str):
                    p = str(ip_str).split(".")
                    return int(p[0]) + (int(p[1]) if len(p) > 1 else 0) / 3
                _ips_p = [__ips(s.get("ip","0")) for s in pc_last5 if s.get("ip")]
                if _ips_p:
                    _avg_ip_p = sum(_ips_p) / len(_ips_p)
                    if _avg_ip_p < 4.5:
                        if is_under:
                            para_parts.append(
                                f"His workload also caps the ceiling — averaging just **{_avg_ip_p:.1f} innings** "
                                f"per outing, he rarely gets deep enough to rack up big strikeout totals."
                            )
                        else:
                            para_parts.append(
                                f"One concern for the Over: he's averaging only **{_avg_ip_p:.1f} innings** per start "
                                f"recently, meaning early exits could cut short his strikeout opportunities."
                            )
                    elif _avg_ip_p >= 6.0 and not is_under:
                        para_parts.append(
                            f"His workload supports the Over — averaging **{_avg_ip_p:.1f} innings** per start "
                            f"means he typically gets three full looks at the lineup."
                        )
            except Exception:
                pass

    else:
        # Hitting props — pitcher context and lineup spot
        if pitcher_name and pc:
            _pc_era  = pc.get("era") or (pc.get("season_stats") or {}).get("era", "—")
            _pc_whip = pc.get("whip") or (pc.get("season_stats") or {}).get("whip", "—")
            if not is_under and _pc_era and _pc_era != "—":
                try:
                    era_f = float(_pc_era)
                    if era_f >= 4.5:
                        para_parts.append(
                            f"The matchup helps: **{pitcher_name}** carries a **{_pc_era} ERA** / {_pc_whip} WHIP, "
                            f"giving the lineup a favorable environment to produce."
                        )
                    elif era_f <= 3.0:
                        para_parts.append(
                            f"The key risk: **{pitcher_name}** has a **{_pc_era} ERA** — an elite arm that can suppress "
                            f"even hot lineups. Form wins here, but matchup difficulty is real."
                        )
                    else:
                        para_parts.append(f"Opposing **{pitcher_name}** ({_pc_era} ERA) is a manageable matchup.")
                except (ValueError, TypeError):
                    pass

        # ── BvP: this batter's personal history vs tonight's pitcher ──────────
        # The single most specific signal — pull it into the prose, not just the
        # matchup box. Frame it relative to the prop side.
        if bvp_ab >= 3 and pitcher_name:
            try:
                _bvp_f2 = float("0" + bvp_avg) if str(bvp_avg).startswith(".") else float(bvp_avg)
            except (ValueError, TypeError):
                _bvp_f2 = 0.0
            _k_note = f", striking out only {bvp_k} time{'s' if bvp_k != 1 else ''}" if bvp_k else ""
            _hr_note = f" with {bvp_hr} HR" if bvp_hr else ""
            if not is_under and _bvp_f2 >= 0.300:
                para_parts.append(
                    f"And he's **already proven it against this arm** — {_first} is "
                    f"**{bvp.get('hits',0)}/{bvp_ab} ({bvp_avg})** vs {pitcher_name}{_hr_note}{_k_note}. "
                    f"Even an elite pitcher hasn't solved him."
                    if (_pc_era and str(_pc_era) != '—' and _safe_lt(_pc_era, 3.5))
                    else f"And he **owns this matchup** — {_first} is **{bvp.get('hits',0)}/{bvp_ab} ({bvp_avg})** "
                         f"vs {pitcher_name}{_hr_note}{_k_note}."
                )
            elif not is_under and _bvp_f2 <= 0.150:
                para_parts.append(
                    f"One caution: {_first} has **struggled against {pitcher_name}** historically — "
                    f"just **{bvp.get('hits',0)}/{bvp_ab} ({bvp_avg})**{_k_note}."
                )
            elif is_under and _bvp_f2 <= 0.150:
                para_parts.append(
                    f"The history backs the Under — {_first} is just **{bvp.get('hits',0)}/{bvp_ab} ({bvp_avg})** "
                    f"against {pitcher_name}{_k_note}."
                )
            elif is_under and _bvp_f2 >= 0.300:
                para_parts.append(
                    f"One caution for the Under: {_first} has **hit {pitcher_name} well** — "
                    f"**{bvp.get('hits',0)}/{bvp_ab} ({bvp_avg})**{_hr_note}."
                )

        # ── Team H2H: how this exact prop has gone vs tonight's opponent ──────
        _th2 = team_h2h or {}
        _th2_games = int(_th2.get("games", 0) or 0)
        if _th2_games >= 1:
            _th2_hit  = _th2.get("over", 0) if not is_under else _th2.get("under", 0)
            _th2_name = _th2.get("team_name") or opponent or "this team"
            _th2_avg  = _th2.get("avg")
            if _th2_hit >= 1:
                _avg_clause = f", averaging {_th2_avg} {market}" if _th2_avg else ""
                para_parts.append(
                    f"He's also **{_th2_hit}/{_th2_games} on this exact prop vs {_th2_name}** "
                    f"this season{_avg_clause}."
                    + (" Small sample, but it's all gone his way." if _th2_games <= 2 else "")
                )

        # ── Venue split: does he produce more at tonight's location? ──────────
        _cur_venue_avg = home_avg if is_home is True else (away_avg if is_home is False else None)
        _cur_venue_rt  = home_rate if is_home is True else (away_rate if is_home is False else None)
        _oth_venue_avg = away_avg if is_home is True else home_avg
        _venue_word    = "at home" if is_home is True else ("on the road" if is_home is False else None)
        if (_venue_word and _cur_venue_avg is not None and _oth_venue_avg is not None
                and home_games_ct >= 4 and away_games_ct >= 4):
            if _cur_venue_avg > _oth_venue_avg * 1.10:
                _rt_clause = f" and hits the {side_lbl} {_cur_venue_rt:.0f}% of the time there" if _cur_venue_rt is not None else ""
                para_parts.append(
                    f"Venue helps too — he's playing **{_venue_word}**, where he averages "
                    f"**{_cur_venue_avg} {market}** vs {_oth_venue_avg} elsewhere{_rt_clause}."
                )
            elif _cur_venue_avg < _oth_venue_avg * 0.90:
                para_parts.append(
                    f"Worth noting: he's **{_venue_word}** tonight, where he's been weaker "
                    f"(**{_cur_venue_avg} {market}** vs {_oth_venue_avg} at the other venue)."
                )

        # ── Handedness: how he hits this pitcher's throwing hand ─────────────
        _hand_split = (vs_hand_splits or {}).get(pc_hand) if pc_hand in ("L", "R") else None
        if _hand_split and int(_hand_split.get("pa", 0) or 0) >= 30:
            try:
                _hops = float("0" + str(_hand_split["ops"])) if str(_hand_split["ops"]).startswith(".") else float(_hand_split["ops"])
            except (ValueError, TypeError):
                _hops = 0.0
            _hand_word = "left-handers" if pc_hand == "L" else "right-handers"
            if not is_under and _hops >= 0.800:
                para_parts.append(
                    f"The platoon angle is in his favor — he carries a **{_hand_split['ops']} OPS vs {_hand_word}** "
                    f"({_hand_split['pa']} PA), and {pitcher_name} throws {'left' if pc_hand=='L' else 'right'}-handed."
                )
            elif not is_under and _hops <= 0.650:
                para_parts.append(
                    f"The platoon angle is a concern — he's at just **{_hand_split['ops']} OPS vs {_hand_word}** "
                    f"({_hand_split['pa']} PA)."
                )
            elif is_under and _hops <= 0.650:
                para_parts.append(
                    f"The platoon angle supports the Under — he's at just **{_hand_split['ops']} OPS vs {_hand_word}** "
                    f"({_hand_split['pa']} PA)."
                )

        if lineup_spot is not None:
            _pa_est2 = {1:4.5,2:4.4,3:4.2,4:4.1,5:3.9,6:3.8,7:3.7,8:3.6,9:3.5}
            _pa_proj = _pa_est2.get(lineup_spot, 4.0)
            spot_word = {1:"leadoff",2:"2-hole",3:"3-hole",4:"cleanup"}.get(lineup_spot, f"#{lineup_spot}")
            if lineup_spot <= 3:
                para_parts.append(
                    f"Batting **{spot_word}** gives him **~{_pa_proj} projected PA** tonight — "
                    f"maximum opportunity to accumulate stats."
                )
            elif lineup_spot >= 7:
                para_parts.append(
                    f"At **{spot_word}** in the order, he's projected for ~{_pa_proj} PA tonight — "
                    f"fewer opportunities than ideal."
                )

    # 4. Conclusion — state the side clearly, explain the conflict if one exists
    opp_side_lbl = "Over" if is_under else "Under"
    score_val    = grade.get("score", 0) or 0

    if _conflict_note:
        # Data disagrees with requested side — be direct about it
        if score_val >= 3:
            _confidence = (
                f"Bottom line: the data leans **{opp_side_lbl}**, but there are enough signals "
                f"supporting the {side_lbl} to make a case. This is a **lower-conviction play** — "
                f"size down and only take it if you have a strong read on the matchup."
            )
        else:
            _confidence = (
                f"Bottom line: the L10 **average** sits on the **{opp_side_lbl}** side of the line — "
                f"but averages can be skewed by outlier games. Check the hit rates in the performance section "
                f"for a cleaner picture. If you're playing the {side_lbl}, treat it as a high-risk lean (0.5u or less)."
            )
    elif score_val >= 8:
        _confidence = (
            f"Bottom line: the evidence stacks cleanly on the **{side_lbl}**. "
            f"This is one of the cleaner spots on the board tonight."
        )
    elif score_val >= 5:
        _confidence = (
            f"Bottom line: the data consistently supports the **{side_lbl} {line_str}**. "
            f"The edge is real — this is a confident play."
        )
    elif score_val >= 3:
        _confidence = (
            f"Bottom line: the signal leans **{side_lbl}**, but there are conflicting factors present. "
            f"This is a play, not a lock — reduce sizing compared to a cleaner spot."
        )
    else:
        _confidence = (
            f"Bottom line: this is a **weak lean** toward the {side_lbl}. "
            f"The {opp_side_lbl} has real arguments here — if the line moves or the matchup changes, "
            f"this flips. Only play it at reduced size."
        )
    # Assemble so the conclusion ALWAYS survives: the opening line + the Bottom
    # line are mandatory; the middle evidence paragraphs are trimmed (lowest
    # priority dropped first, keeping order) until the whole thing fits 1024.
    verdict_label = f"🎯 VERDICT: {SIDE_UP} {line_str}" + (" ⚠️ (Requested — Data Conflicts)" if _conflict_note else "")
    _head    = f"{quick_stats}\n\n"
    _opening = para_parts[0] if para_parts else ""
    _middle  = para_parts[1:]   # streak, pitcher, BvP, team-H2H, venue, handedness, lineup
    _budget  = 1024 - len(_head) - len(_opening) - len(_confidence) - 4  # 4 = newlines

    _kept_middle = []
    for _p in _middle:
        if len("\n".join(_kept_middle + [_p])) <= _budget:
            _kept_middle.append(_p)
    narrative = "\n".join(filter(None, [_opening] + _kept_middle + [_confidence]))

    _verdict_body = f"{_head}{narrative}"
    if len(_verdict_body) > 1024:
        _verdict_body = _verdict_body[:1021] + "..."
    embed.add_field(
        name=verdict_label,
        value=_verdict_body,
        inline=False,
    )

    # ── LAYER 5b: Recent ──────────────────────────────────────────────────────
    l5_disp  = _hfmt(l5,  is_under)
    l10_disp = _hfmt(l10, is_under)
    l20_disp = _hfmt(l20, is_under)
    embed.add_field(
        name="— performance",
        value=(
            f"L5 **{l5_disp}** · L10 **{l10_disp}** · L20 **{l20_disp}**\n"
            f"Season avg **{season_avg}** over **{gp} GP**\n"
            f"Last 5 values: `{recent_log}`"
        ),
        inline=False,
    )

    # ── LAYER 5c: Matchup box ─────────────────────────────────────────────────
    matchup_box = []
    if opponent:
        matchup_box.append(f"**{opponent}**" + (f" — #{opp_k_rank}/30 in K rate ({opp_k_pct*100:.1f}%)" if opp_k_rank and opp_k_pct else ""))
    if pc:
        pc_name_lbl = pc.get("name") or pc.get("pitcher_name", "")
        pc_hand     = pc.get("hand", "")
        hand_str    = f" ({pc_hand}HP)" if pc_hand else ""
        ss = pc.get("season_stats") or {}
        if prop_type == "strikeouts":
            matchup_box.append(
                f"Facing **{pc_name_lbl}**{hand_str} — "
                f"**{ss.get('era','—')}** ERA · **{ss.get('k_per_9','—')}** K/9 · "
                f"**{ss.get('k_per_gs','—')}** K/start avg"
            )
            if pc_last5:
                last5_str = "  ".join(f"{s.get('k',s.get('value','?'))}K" for s in pc_last5)
                matchup_box.append(f"Last 5 starts: `{last5_str}`")
        else:
            # Hitting prop — show ERA, HR/9, FIP
            pc_hr9  = pc.get("hr_per_9") or ss.get("hr_per_9", "—")
            pc_fip  = pc.get("fip")      or ss.get("fip",      "—")
            matchup_box.append(
                f"Facing **{pc_name_lbl}**{hand_str} — "
                f"**{ss.get('era','—')}** ERA · **{pc_hr9}** HR/9 · **{pc_fip}** FIP"
            )
        if pc_home_era is not None and pc_away_era is not None:
            matchup_box.append(f"Home ERA **{pc_home_era}** · Away ERA **{pc_away_era}**")
    if bvp_ab >= 5:
        matchup_box.append(f"H2H: {bvp_h2h_str} ({bvp_ab} AB)")

    # Team career BvP
    if team_bvp and team_bvp.get("pa", 0) >= 5:
        try: tb_avg = float(team_bvp.get("avg", 0) or 0)
        except (TypeError, ValueError): tb_avg = 0.0
        tb_pa = team_bvp.get("pa", 0)
        try: tb_ops = float(team_bvp.get("ops", 0) or 0)
        except (TypeError, ValueError): tb_ops = 0.0
        opp_nm  = opponent or "opp team"
        matchup_box.append(
            f"🧠 Career vs {opp_nm}: **{tb_avg:.3f}** avg / **{tb_pa}** PA · OPS **{tb_ops:.3f}**"
        )

    # Team H2H prop history — how often has THIS specific prop gone Over/Under vs this team
    _th = team_h2h or {}
    _th_games = int(_th.get("games", 0) or 0)
    if _th_games >= 1:
        _th_over  = _th.get("over", 0)
        _th_under = _th.get("under", 0)
        _th_oname = _th.get("team_name") or opponent or "this team"
        _th_avg   = _th.get("avg", 0)
        _th_line  = f"{float(line):g}"
        # Side-specific framing — show from the requested side's perspective
        if is_under:
            _th_hits  = _th_under
            _th_rate  = _th.get("under_rate", 0)
            _th_icon  = "✅" if _th_rate >= 60 else ("🔴" if _th_rate <= 35 else "➡️")
        else:
            _th_hits  = _th_over
            _th_rate  = _th.get("over_rate", 0)
            _th_icon  = "✅" if _th_rate >= 60 else ("🔴" if _th_rate <= 35 else "➡️")
        _sample_note = "" if _th_games >= 5 else f" ⚠️ small sample ({_th_games}g)"
        matchup_box.append(
            f"{_th_icon} **{side_lbl} {_th_line} vs {_th_oname}** this season: "
            f"**{_th_hits}/{_th_games}** hit ({_th_rate:.0f}%) · avg **{_th_avg}**{_sample_note}"
        )

    if not matchup_box:
        matchup_box.append("No extended matchup data available.")

    embed.add_field(name="— vs matchup", value="\n".join(matchup_box), inline=False)

    # ── ENVIRONMENT: park · weather · defense · bullpen · umpire ─────────────
    env_lines = []

    # Park factor — polarity depends on prop type
    if park_factor and park_factor != 1.0:
        if prop_type == "strikeouts":
            # For K props: hitter-friendly parks don't strongly affect Ks,
            # but pitcher-friendly parks can keep offenses off-balance → more Ks
            if park_factor >= 1.05:
                pf_icon = "🔴" if not is_under else "🟢"
                env_lines.append(
                    f"{pf_icon} **Hitter-friendly park** ({park_factor:.2f}x) — "
                    f"{'⚠️ longer at-bats may suppress Ks' if is_under else '✅ more offense = deeper counts = K chances'}"
                )
            elif park_factor <= 0.95:
                pf_icon = "🟢" if not is_under else "🔴"
                env_lines.append(
                    f"{pf_icon} **Pitcher-friendly park** ({park_factor:.2f}x) — "
                    f"{'✅ suppressed offense supports pitcher dominance' if not is_under else '⚠️ pitchers tend to go deeper here = more K exposure'}"
                )
            else:
                env_lines.append(f"🏟️ Neutral park ({park_factor:.2f}x)")
        else:
            if park_factor >= 1.05:
                pf_icon = "🟢" if not is_under else "🔴"
                env_lines.append(f"{pf_icon} **Hitter-friendly park** ({park_factor:.2f}x) — {'✅ boosts production' if not is_under else '⚠️ elevates risk for Under'}")
            elif park_factor <= 0.95:
                pf_icon = "🔴" if not is_under else "🟢"
                env_lines.append(f"{pf_icon} **Pitcher-friendly park** ({park_factor:.2f}x) — {'⚠️ suppresses offense' if not is_under else '✅ supports Under'}")
            else:
                env_lines.append(f"🏟️ Slightly hitter-friendly ({park_factor:.2f}x)")

    # Weather / wind
    if weather:
        if weather.get("dome"):
            env_lines.append("🏟️ Indoor — weather N/A")
        else:
            w_spd    = weather.get("speed_mph", 0)
            w_effect = weather.get("effect", "")
            friendly = weather.get("hitter_friendly")
            temp     = weather.get("temp_f")
            if w_spd and w_spd >= 5:
                icon = "💨" if friendly else ("🛑" if friendly is False else "🌬️")
                env_lines.append(f"{icon} Wind: **{w_spd} mph** {w_effect}")
            if temp:
                env_lines.append(f"🌡️ Temp: **{temp}°F**")

    # Opponent defense OAA
    if oaa and oaa.get("oaa") is not None:
        oaa_val = oaa["oaa"]
        if oaa_val <= -10:   oaa_lbl = "poor defense — balls find grass ✓"
        elif oaa_val <= -5:  oaa_lbl = "below-avg defense ✓"
        elif oaa_val >= 10:  oaa_lbl = "elite defense ✗"
        elif oaa_val >= 5:   oaa_lbl = "above-avg defense ✗"
        else:                oaa_lbl = "average defense"
        env_lines.append(f"🧤 Opp defense: **{oaa_val:+d} OAA** — {oaa_lbl}")

    # Bullpen
    if bullpen and bullpen.get("era") is not None:
        era  = bullpen["era"]
        whip = bullpen.get("whip", "?")
        hr9  = bullpen.get("hr9", "?")
        fat  = bullpen.get("fatigued_count", 0)
        pen_icon = "🔥" if era >= 4.5 else ("🛡️" if era <= 3.0 else "⚪")
        pen_line = f"{pen_icon} Opp bullpen (L7): **{era} ERA** · {whip} WHIP · {hr9} HR/9"
        if fat >= 2:
            pen_line += f"\n⚠️ **{fat} tired arms** in pen (appeared last 3 days)"
        elif fat == 1:
            pen_line += f" · 1 arm used recently"
        env_lines.append(pen_line)

    # Umpire
    if umpire and umpire.get("name"):
        ump_name = umpire["name"]
        k_boost  = umpire.get("k_boost")
        if k_boost is not None:
            boost_str = f"+{k_boost:.1f}%" if k_boost >= 0 else f"{k_boost:.1f}%"
            if k_boost >= 3:
                ump_tag = f"📈 strikeout-friendly ({boost_str} vs avg)"
            elif k_boost <= -3:
                ump_tag = f"📉 contact zone ({boost_str} vs avg)"
            else:
                ump_tag = f"⚖️ neutral zone ({boost_str})"
            env_lines.append(f"⚖️ HP Ump: **{ump_name}** — {ump_tag}")
        else:
            env_lines.append(f"⚖️ HP Ump: **{ump_name}**")

    if env_lines:
        env_text = "\n".join(env_lines)
        if len(env_text) > 1024:
            env_text = env_text[:1021] + "..."
        embed.add_field(name="— environment", value=env_text, inline=False)

    # ── VORTEX-AUDITOR: data integrity checks ─────────────────────────────────
    _audit_flags: list[str] = []
    try:
        _l10f     = float(l10_avg) if l10_avg and l10_avg not in (0, "—", "") else None
        _linef    = float(line)
        _season_f = float(season_avg) if season_avg and season_avg not in (0, "—", "") else None

        # 1 — Prop-type plausibility (catches OCR misidentification like Buxton H+R+RBI → Runs Scored)
        _LOW_THRESH = {
            "hits_runs_rbis": (0.8,  "H+R+RBI props typically average 1.5+ per game"),
            "total_bases":    (0.4,  "TB props typically average 0.8+ per game"),
            "hits":           (0.15, "Hits props typically average 0.5+ per game"),
            "runs_scored":    (0.2,  "Runs props typically average 0.3+ per game for regulars"),
            "fantasy_score":  (3.0,  "Fantasy Score (PP) props typically average 5+ per game"),
        }
        if _l10f is not None and (l10.get("games") or 0) >= 5:
            _thr, _reason = _LOW_THRESH.get(prop_type, (None, ""))
            if _thr is not None and _l10f < _thr:
                _audit_flags.append(
                    f"⚠️ **STAT CHECK** — L10 avg of **{l10_avg}** for **{prop_type.replace('_',' ').title()}** "
                    f"is suspiciously low ({_reason}). "
                    f"OCR may have detected the wrong prop type — "
                    f"re-run with `/prediction` using the correct stat abbreviation to confirm."
                )

        # 2 — L5 vs L10 extreme divergence (recent form spike or collapse)
        _l5r_eff  = (100 - l5_rate_raw)  if is_under else l5_rate_raw
        _l10r_eff = (100 - l10_rate_raw) if is_under else l10_rate_raw
        if l5.get("games", 0) >= 5 and l10.get("games", 0) >= 8:
            _div = abs(_l5r_eff - _l10r_eff)
            if _div >= 45:
                _dir = "surged" if _l5r_eff > _l10r_eff else "collapsed"
                _audit_flags.append(
                    f"📈 **TREND SHIFT** — L5 rate ({_l5r_eff:.0f}%) vs L10 rate ({_l10r_eff:.0f}%) "
                    f"diverge by {_div:.0f}pp. Recent form has {_dir} sharply — "
                    f"weight the L5 data more heavily when evaluating confidence."
                )

        # 3 — Season avg vs L10 avg large divergence
        if _l10f is not None and _season_f is not None and _season_f > 0:
            _pct_shift = abs(_l10f - _season_f) / _season_f
            if _pct_shift >= 0.60 and abs(_l10f - _season_f) >= 0.6:
                _dir2 = "above" if _l10f > _season_f else "below"
                _audit_flags.append(
                    f"📊 **PACE SHIFT** — L10 avg (**{l10_avg}**) is significantly {_dir2} season avg "
                    f"(**{season_avg}**). Recent output has diverged from baseline — "
                    f"{'hot streak or role change' if _l10f > _season_f else 'slump or injury impact'}."
                )

        # 4 — K-prop: model projection vs L10 large gap (projection model vs observed data conflict)
        if prop_type == "strikeouts" and _proj_k_for_audit is not None and _l10f is not None:
            _pgap = abs(_proj_k_for_audit - _l10f)
            if _pgap >= 1.8:
                _pdir = "above" if _proj_k_for_audit > _l10f else "below"
                _audit_flags.append(
                    f"🔢 **PROJECTION GAP** — Model projects **{_proj_k_for_audit} Ks** but L10 avg is "
                    f"**{l10_avg} Ks** — a {_pgap:.1f}-K gap. "
                    f"The model is driven by K/9 × opponent K rate; the L10 reflects actual results. "
                    f"Large divergence suggests{'  opponent adjustment' if _proj_k_for_audit > _l10f else ' pitcher underperformance vs model'}."
                )

        # 5 — Line vs L10 avg extreme mismatch (line is far outside any reasonable range)
        if _l10f is not None and _linef > 0 and _l10f > 0:
            _ratio = _linef / _l10f
            if _ratio >= 3.0 and _l10f < 1.0:
                _audit_flags.append(
                    f"🚩 **LINE CHECK** — The **{line_str}** line is {_ratio:.1f}× the L10 avg (**{l10_avg}**). "
                    f"This extreme gap may indicate the prop type was misidentified — "
                    f"verify against the original slip before betting."
                )

        # 6 — Missing critical data for the prop type
        if prop_type in ("hits_runs_rbis", "total_bases", "hits", "fantasy_score") and (l10.get("games") or 0) < 5:
            _audit_flags.append(
                f"📭 **THIN DATA** — Fewer than 5 games in L10 sample for {prop_type.replace('_', ' ')} prop. "
                f"Hit rate and avg are not statistically reliable — treat projections with caution."
            )
        if prop_type == "strikeouts" and not pc:
            _audit_flags.append(
                "📭 **NO PITCHER DATA** — Could not load pitcher card. "
                "K projection and K/9 analysis are unavailable — base decision on opponent K rate only."
            )

    except Exception:
        pass  # auditor must never crash the card

    if _audit_flags:
        _audit_text = "\n".join(_audit_flags)
        if len(_audit_text) > 1024:
            _audit_text = _audit_text[:1021] + "..."
        embed.add_field(name="— audit flags", value=_audit_text, inline=False)

    # ── LAYER 6: Risk & Legend ─────────────────────────────────────────────────
    # Penalty descriptions from grade_pick() take priority — show them first.
    penalty_desc  = grade.get("penalty_desc") or []
    force_capped  = grade.get("force_capped", False)

    risk_lines = []
    # Bullpen game warning (reliever listed as starter)
    if pc and pc.get("validated_role") in ("reliever", "closer", "unknown"):
        pc_name = pc.get("name") or "The listed pitcher"
        risk_lines.append(
            f"⚠️ **Bullpen game** — {pc_name} is a reliever. Expect multiple arms."
        )
    # Bullpen fatigue
    if bullpen and bullpen.get("fatigued_count", 0) >= 3:
        fat = bullpen["fatigued_count"]
        risk_lines.append(
            f"🔥 **Pen fatigue** — {fat} relievers active last 3 days. Closer may be unavailable."
        )
    # Umpire impact on strikeout props
    if umpire and umpire.get("k_boost") is not None and prop_type == "strikeouts":
        k_boost = umpire["k_boost"]
        if k_boost <= -5:
            risk_lines.append(f"⚠️ Umpire tends to squeeze the zone ({k_boost:.1f}% K rate) — works against the K total.")
    # Variance / stability flag
    stab_tier_risk = grade.get("stability_tier", "")
    if stab_tier_risk == "VOLATILE":
        risk_lines.append("⚡ **Volatile output** — high stdev in recent games. Boom-or-bust player; size down.")
    elif stab_tier_risk == "LOW":
        risk_lines.append("📉 **Low stability** — inconsistent recent values. Treat hit rate with caution.")
    for p in penalty_desc:
        risk_lines.append(p)
    if force_capped:
        risk_lines.append("🚫 **Downgraded → Good** — 2+ risk flags active. Elite/Strong blocked.")
    # Fill remaining slots with data-derived risk flags (up to 2 total)
    for r in risks:
        if len(risk_lines) >= 3:
            break
        # Skip if a penalty line already covers the same point
        if "L5" in r and any("L5" in p for p in penalty_desc):
            continue
        risk_lines.append(f"• {r}")
    if not risk_lines:
        risk_lines.append("• No major risk flags detected.")

    _risk_text = "\n".join(risk_lines)
    if len(_risk_text) > 1024:
        _risk_text = _risk_text[:1021] + "..."
    embed.add_field(name="— risk", value=_risk_text, inline=False)

    ratings_raw = "💎 Elite (10+) · 🔥 Strong (6-9) · ✅ Good (3-5) · ➡️ Lean (0-2) · ⚠️ Risky (<0) · 🚫 Fade (stay away)"
    bold_map    = {"Elite": "💎", "Strong": "🔥", "Good": "✅", "Lean": "➡️", "Risky": "⚠️", "Fade": "🚫"}
    if label in bold_map:
        tag = f"{bold_map[label]} {label}"
        ratings_raw = ratings_raw.replace(tag, f"**{tag}**")

    embed.add_field(
        name="— rating scale",
        value=f"{ratings_raw}\n*Higher score = more data behind the pick.*",
        inline=False,
    )

    # ── Model Verdict vs User Selection ────────────────────────────────────────
    if side_comparison:
        mv = side_comparison["model_verdict"]
        mv_lbl = "Over" if mv == "over" else "Under"
        over_s = side_comparison["over_score"]
        under_s = side_comparison["under_score"]
        conf = side_comparison["confidence"]

        if mv != user_side:
            # Model disagrees with user selection
            embed.add_field(
                name=f"⚠️ You selected {user_side_lbl} — Model favors {mv_lbl}",
                value=(
                    f"Over score **{over_s}** · Under score **{under_s}**\n"
                    f"Model recommends **{mv_lbl}** ({conf:.0%} confidence) — "
                    f"the data above supports the **{mv_lbl}** case."
                ),
                inline=False,
            )
        else:
            # Model agrees with user selection
            embed.add_field(
                name=f"✅ Model Confirms: {side_lbl}",
                value=(
                    f"Over score **{over_s}** · Under score **{under_s}** · "
                    f"Confidence: **{conf:.0%}**"
                ),
                inline=False,
            )

    import vortextime
    from datetime import date as _date
    _y, _m, _d = vortextime.vortex_day().split("-")
    import calendar
    today_str = f"{calendar.month_name[int(_m)]} {int(_d)}, {_y}"
    footer = f"VORTEX"
    if game_time:
        footer += f" · {game_time}"
    footer += f" · {unit} · {today_str}"
    if book_str:
        footer += f" · {book_str}"
    embed.set_footer(text=footer)
    return embed
