"""
MaxOverlay POE2 — screen-reading price-check overlay for Path of Exile 2.

Built for cloud gaming (e.g. Boosteroid), where the clipboard never reaches
your machine, so it works purely from pixels. Works on local installs too.
PoE1 support may come later; everything game-specific is the data sources.

Pipeline:
  F5 -> capture the whole screen -> native Windows OCR (with positions) ->
  find the item by NAME among all screen text -> query the trade2 API
  (or poe2scout for currency) -> show prices in a floating overlay.

Hotkeys (configurable): F5 = price check | F6 = open web | F8 = refresh DB
                        ESC = hide | Ctrl+Shift+Q = quit
"""

import asyncio
import ctypes
import difflib
import json
import os
import re
import statistics
import sys
import threading
import tkinter as tk
import webbrowser

import mss
import requests
from PIL import Image

APP_NAME = "MaxOverlay POE2"
APP_ID = "MaxOverlay-POE2"      # filesystem/User-Agent-safe form (no spaces)
VERSION = "1.1.0"

# Cache/temp dir in %LOCALAPPDATA% (user-writable, no admin needed).
TMP = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                   APP_ID)
CROP_PATH = os.path.join(TMP, "tooltip_crop.png")
DB_PATH = os.path.join(TMP, "items_db.json")
CONFIG_PATH = os.path.join(TMP, "config.json")
os.makedirs(TMP, exist_ok=True)

# ── User config ───────────────────────────────────────────────────────────────
# Editable JSON in %LOCALAPPDATA%\MaxOverlay\config.json. The ⚙ button
# opens an in-app Settings window (league, hotkeys, behavior) that writes here
# and applies live — no restart needed.

_DEFAULT_CONFIG = {
    "league": None,         # null = auto-detect the current league
    "near_cursor": True,    # overlay pops up next to the cursor (Awakened-style)
    "pill": True,           # tiny status badge shown while the overlay sleeps
    "hotkeys": {
        "check": "f5",      # price-check the item under the cursor
        "web": "f6",        # open the search on the trade website
        "refresh": "f8",    # refresh the item/stat databases
        "hide": "esc",      # hide the overlay
        "quit": "ctrl+shift+q",
    },
}


def load_config() -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in _DEFAULT_CONFIG.items()}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if k == "hotkeys" and isinstance(v, dict):
                    cfg["hotkeys"].update(v)
                else:
                    cfg[k] = v
        else:
            save_config(cfg)
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ── Capture ───────────────────────────────────────────────────────────────────

def cursor_pos():
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def capture_screen():
    """Capture the primary monitor. Returns (img, offset_x, offset_y)."""
    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, mon["left"], mon["top"]


# ── OCR (native Windows OCR via winrt) ────────────────────────────────────────

_ocr_engine = None

async def _ocr_async(path: str, with_pos=False):
    global _ocr_engine
    import winrt.windows.media.ocr as ocr
    import winrt.windows.storage as storage
    import winrt.windows.graphics.imaging as imaging

    f = await storage.StorageFile.get_file_from_path_async(path)
    stream = await f.open_async(storage.FileAccessMode.READ)
    decoder = await imaging.BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    if _ocr_engine is None:
        _ocr_engine = ocr.OcrEngine.try_create_from_user_profile_languages()
    if _ocr_engine is None:
        return []
    result = await _ocr_engine.recognize_async(bitmap)
    if not with_pos:
        return [ln.text for ln in result.lines]
    # With positions: bounding box of each line (from its words)
    out = []
    for ln in result.lines:
        xs = ys = 1e9
        xe = ye = -1e9
        for w in ln.words:
            r = w.bounding_rect
            xs = min(xs, r.x); ys = min(ys, r.y)
            xe = max(xe, r.x + r.width); ye = max(ye, r.y + r.height)
        if xe < 0:
            continue
        out.append({"text": ln.text, "x": xs, "y": ys,
                    "w": xe - xs, "h": ye - ys,
                    "cx": (xs + xe) / 2.0, "cy": (ys + ye) / 2.0})
    return out


def run_ocr(img: Image.Image) -> list[str]:
    # winrt StorageFile REQUIRES backslashes and a path with no odd characters
    w, h = img.size
    img.resize((w * 2, h * 2), Image.LANCZOS).save(CROP_PATH)
    return asyncio.run(_ocr_async(CROP_PATH))


def run_ocr_fullscreen(img: Image.Image, scale: float = 2.0) -> list[dict]:
    """OCR the whole screen. Returns lines with positions in original coords."""
    w, h = img.size
    path = os.path.join(TMP, "fullscreen.png")
    img.resize((int(w * scale), int(h * scale)), Image.LANCZOS).save(path)
    lines = asyncio.run(_ocr_async(path, with_pos=True))
    for ln in lines:                       # back to original screen coords
        for k in ("x", "y", "w", "h", "cx", "cy"):
            ln[k] /= scale
    return lines


# ── Item database (fuzzy match against OCR) ───────────────────────────────────

ITEMS_URL = "https://www.pathofexile.com/api/trade2/data/items"

HEADERS = {
    "User-Agent": f"{APP_ID}/{VERSION} (price-check overlay)",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def load_item_db(force=False) -> list[dict]:
    """Return a list of {text, name, type, unique, norm} for every item."""
    db = None
    if not force and os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            db = None
    if db is None:
        r = requests.get(ITEMS_URL, headers=HEADERS, timeout=10)
        db = []
        for cat in r.json().get("result", []):
            cat_label = cat.get("label", "").lower()  # currency, gems, armour...
            for e in cat.get("entries", []):
                text = e.get("text") or e.get("type") or e.get("name")
                if not text:
                    continue
                db.append({
                    "text": text,
                    "name": e.get("name", ""),
                    "type": e.get("type", ""),
                    "unique": bool(e.get("flags", {}).get("unique")),
                    "category": cat_label,
                })
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f)
    for e in db:
        e["norm"] = _norm(e["text"])
    return db


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


# ── Inverted index + fast matcher ─────────────────────────────────────────────
#
# difflib against thousands of entries is O(n) per line -> ~10s. Instead we
# index each entry by its words: for an OCR line we only score the entries
# that share at least one word (tens, not thousands).

_STOP = {"to", "of", "the", "a", "an", "in", "your", "and", "per", "on", "as", "by"}


def _words(norm: str):
    return [w for w in norm.replace("#", " ").split() if len(w) >= 3 and w not in _STOP]


def build_index(entries: list[dict]) -> dict:
    idx = {}
    for i, e in enumerate(entries):
        for w in set(_words(e["norm"])):
            idx.setdefault(w, []).append(i)
    return idx


def _fast_best(query_norm: str, entries: list[dict], index: dict, topk=80):
    """Return (entry, score) using the index to pre-filter candidates."""
    qwords = _words(query_norm)
    if not qwords:
        return None, 0.0
    from collections import Counter
    cnt = Counter()
    for w in qwords:
        for i in index.get(w, ()):
            cnt[i] += 1
    if not cnt:
        return None, 0.0
    best, bs = None, 0.0
    for i, _ in cnt.most_common(topk):
        e = entries[i]
        sc = difflib.SequenceMatcher(None, query_norm, e["norm"]).ratio()
        if query_norm in e["norm"] or e["norm"] in query_norm:
            sc += 0.12
        # Prefer unique items over base types on tie
        if e.get("unique"):
            sc += 0.05
        if sc > bs:
            bs, best = sc, e
    return best, bs


def _extract_gem_level(ocr_lines) -> int | None:
    """Extract a gem's level from OCR (looks for 'LEVEL XX' or 'LVL XX')."""
    for line in ocr_lines[:8]:
        m = re.search(r'\b(?:LEVEL|LVL)\s*(\d+)', line, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def resolve_fullscreen(lines_pos, db, index):
    """
    Find the item by searching its NAME among ALL lines on the screen.
    An item name matches the DB with a high score; chat/UI/minimap do not.
    Returns (item, tooltip_lines, guess):
      - item: matched DB entry, or None if no confident match
      - tooltip_lines: the tooltip's text lines (name + mods), top-to-bottom
      - guess: best-effort 'what the OCR read' hint (shown when item is None)
    """
    if not db or not lines_pos:
        return None, [], ""

    # 1) Best name match among single lines AND adjacent vertical pairs
    #    (the name often spans 2 lines: "The Ordained" / "Grand Spear").
    best, best_score, best_i = None, 0.0, -1
    for i, l in enumerate(lines_pos):
        nc = _norm(l["text"])
        if len(nc) < 3:
            continue
        e, sc = _fast_best(nc, db, index)
        if e and sc > best_score:
            best_score, best, best_i = sc, e, i
    for i in range(len(lines_pos) - 1):
        a, b = lines_pos[i], lines_pos[i + 1]
        # only pair lines that are vertically adjacent and aligned
        if (abs(b["y"] - (a["y"] + a["h"])) < a["h"] * 1.8 and
                abs(a["cx"] - b["cx"]) < max(a["w"], b["w"], 1)):
            nc = _norm(a["text"] + " " + b["text"])
            e, sc = _fast_best(nc, db, index)
            if e and sc > best_score:
                best_score, best, best_i = sc, e, i

    if not best or best_score < 0.60:
        # No confident match: return what the OCR likely read near the top,
        # plus the closest DB name, as a hint for the user.
        guess = lines_pos[best_i]["text"] if best_i >= 0 else (
            lines_pos[0]["text"] if lines_pos else "")
        if best and best_i >= 0:
            guess += f"  (closest: {best.get('name') or best.get('type')} {best_score:.0%})"
        return None, [], guess

    # 2) Collect the tooltip lines: those in the SAME COLUMN as the name
    #    (close center X) and BELOW (or level with) it, until a large vertical
    #    gap appears (end of the tooltip).
    name = lines_pos[best_i]
    col = name["cx"]
    max_y = name["y"] + 820            # max tooltip height
    near = [l for l in lines_pos
            if name["y"] - name["h"] <= l["y"] <= max_y and abs(l["cx"] - col) < 360]
    near.sort(key=lambda l: l["y"])

    # Break on a large vertical gap (end of tooltip). Internal dividers leave
    # small gaps (~40px); the jump to chat/other UI is larger.
    gap_limit = max(130, name["h"] * 6)
    tip, prev_bottom = [], None
    bx0 = by0 = 1e9; bx1 = by1 = -1e9   # tooltip bounding box (screen coords)
    for l in near:
        if prev_bottom is not None and l["y"] - prev_bottom > gap_limit:
            break
        tip.append(l["text"])
        prev_bottom = l["y"] + l["h"]
        bx0 = min(bx0, l["x"]); by0 = min(by0, l["y"])
        bx1 = max(bx1, l["x"] + l["w"]); by1 = max(by1, l["y"] + l["h"])

    item = {**best, "match_score": round(best_score, 2)}
    if bx1 > bx0:
        # Where the game tooltip sits on screen — used to keep the overlay off it
        item["tooltip_bbox"] = (int(bx0), int(by0), int(bx1), int(by1))
    # Gem with a level: same logic, using the tooltip lines
    if ("uncut" in item.get("type", "").lower() and "gem" in item.get("type", "").lower()
            and "uncut" in " ".join(tip).lower()):
        lvl = _extract_gem_level(tip)
        if lvl:
            leveled = _norm(f'{item["type"]} (Level {lvl})')
            e2, sc2 = _fast_best(leveled, db, index)
            if e2 and sc2 > 0.8:
                item = {**e2, "match_score": round(sc2, 2), "gem_level": lvl}
            else:
                item["gem_level"] = lvl
    return item, tip, ""


def resolve_item(ocr_lines, db, index) -> dict | None:
    if not db:
        return None
    head = [l for l in ocr_lines[:8] if len(_norm(l)) >= 3]
    candidates = list(head)
    for i in range(len(head) - 1):
        candidates.append(head[i] + " " + head[i + 1])

    best, best_score = None, 0.0
    for cand in candidates:
        nc = _norm(cand)
        e, sc = _fast_best(nc, db, index)
        if e and sc > best_score:
            best_score, best = sc, e
    if best and best_score >= 0.55:
        # Uncut gems with a level: ONLY if "uncut" appears in the OCR (prevents
        # a weapon's "Grants Skill: Level 19" from matching a gem by mistake).
        raw_lower = " ".join(ocr_lines).lower()
        if ("uncut" in best.get("type", "").lower() and "gem" in best.get("type", "").lower()
                and "uncut" in raw_lower):
            lvl = _extract_gem_level(ocr_lines)
            if lvl:
                leveled_type = f'{best["type"]} (Level {lvl})'
                nc = _norm(leveled_type)
                e2, sc2 = _fast_best(nc, db, index)
                if e2 and sc2 > 0.8:
                    return {**e2, "match_score": round(sc2, 2), "gem_level": lvl}
                # If the exact level isn't found, keep the level anyway
                return {**best, "match_score": round(best_score, 2), "gem_level": lvl}
        return {**best, "match_score": round(best_score, 2)}
    return None


# ── Stats database (match mods from OCR) ──────────────────────────────────────

STATS_URL = "https://www.pathofexile.com/api/trade2/data/stats"
STATS_PATH = os.path.join(TMP, "stats_db.json")


def load_stat_db(force=False) -> list[dict]:
    """List of {id, text, norm, group} for every searchable stat."""
    if not force and os.path.exists(STATS_PATH):
        try:
            with open(STATS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    r = requests.get(STATS_URL, headers=HEADERS, timeout=10)
    out = []
    for grp in r.json().get("result", []):
        label = grp.get("label", "")
        for e in grp.get("entries", []):
            txt = e.get("text", "")
            if not e.get("id") or not txt:
                continue
            out.append({
                "id": e["id"],
                "text": txt,
                "norm": _stat_norm(txt),
                "group": label,
            })
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def _stat_norm(s: str) -> str:
    """Normalize a stat text: numbers -> #, no punctuation, lowercase."""
    s = s.lower()
    s = re.sub(r"[+\-]?\d+\.?\d*", "#", s)   # numbers -> #
    s = re.sub(r"[^a-z# ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Mods with no relevant numeric value, or noise that doesn't help the search
_STAT_SKIP = re.compile(r"requires|quality|corrupted|sockets|item level|^level \d", re.I)


def match_stats(ocr_lines, stat_db, index, min_score=0.82) -> list[dict]:
    """
    For each OCR mod line, find its stat_id, value and group
    (Explicit/Implicit/...). Preserves the TOOLTIP ORDER (= in-game order:
    implicit first, then explicits), without reordering by score.
    """
    if not stat_db:
        return []
    out, seen = [], set()
    for line in ocr_lines:
        if len(line) < 6 or _STAT_SKIP.search(line):
            continue
        nums = re.findall(r"\d+\.?\d*", line)
        nline = _stat_norm(line)
        best, best_s = _fast_best(nline, stat_db, index)
        if best and best_s >= min_score and best["id"] not in seen:
            seen.add(best["id"])
            out.append({
                "id": best["id"], "value": float(nums[0]) if nums else None,
                "text": best["text"], "score": round(best_s, 2),
                "group": best.get("group", "Explicit"),
            })
    return out


# ── Trade API ─────────────────────────────────────────────────────────────────

LEAGUES_URL  = "https://www.pathofexile.com/api/trade2/data/leagues"
SEARCH_URL   = "https://www.pathofexile.com/api/trade2/search/{league}"
FETCH_URL    = "https://www.pathofexile.com/api/trade2/fetch/{ids}?query={qid}"
WEB_URL      = "https://www.pathofexile.com/trade2/search/{league}/{qid}"
EXCHANGE_URL = "https://www.pathofexile.com/api/trade2/exchange/poe2/{league}"
STATIC_URL   = "https://www.pathofexile.com/api/trade2/data/static"

# poe2scout: community API with real in-game Currency Exchange prices
# (volume-weighted, like the ratio the game shows).
SCOUT_BASE     = "https://poe2scout.com/api"
SCOUT_REALM    = "poe2"
SCOUT_CURR_URL = SCOUT_BASE + "/{realm}/Leagues/{league}/Currencies/{apiid}"

# ── Currency Exchange (name -> id map for the exchange API) ───────────────────

_static_map = None   # {normalized_name: exchange_id}

def _load_static_map():
    global _static_map
    if _static_map is not None:
        return _static_map
    cache_path = os.path.join(TMP, "static_map.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                _static_map = json.load(f)
                return _static_map
        except Exception:
            pass
    try:
        r = requests.get(STATIC_URL, headers=HEADERS, timeout=10)
        smap = {}
        for grp in r.json().get("result", []):
            for e in grp.get("entries", []):
                eid = e.get("id", "")
                text = e.get("text", "")
                if eid and text and eid != "sep":
                    smap[_norm(text)] = eid
        _static_map = smap
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(smap, f)
    except Exception:
        _static_map = {}
    return _static_map


def _find_exchange_id(item_name: str) -> str | None:
    """Find the exchange ID for a currency/gem name."""
    smap = _load_static_map()
    norm = _norm(item_name)
    # Exact match
    if norm in smap:
        return smap[norm]
    # Fuzzy: find the closest one
    best_id, best_sc = None, 0.0
    for key, eid in smap.items():
        sc = difflib.SequenceMatcher(None, norm, key).ratio()
        if sc > best_sc:
            best_sc, best_id = sc, eid
    return best_id if best_sc >= 0.75 else None


def search_poe2scout(item: dict, league: str) -> dict:
    """
    Currency price from poe2scout (the REAL in-game Currency Exchange price,
    volume-weighted). Returns the price in exalted.
    """
    import urllib.parse
    apiid = _find_exchange_id(item.get("type") or item.get("text", ""))
    if not apiid:
        return {"error": f"No ApiId for '{item.get('type', '?')}'"}

    ck = json.dumps({"scout": True, "apiid": apiid, "league": league}, sort_keys=True)
    cached = _search_cache.get(ck)
    if cached:
        ts, result = cached
        if _time.time() - ts < _CACHE_TTL:
            return result

    try:
        url = SCOUT_CURR_URL.format(realm=SCOUT_REALM,
                                    league=urllib.parse.quote(league), apiid=apiid)
        r = requests.get(url, headers={"User-Agent": f"{APP_ID}/{VERSION}"}, timeout=10)
        if r.status_code != 200:
            return {"error": f"poe2scout HTTP {r.status_code}"}
        data = r.json()
        logs = data.get("PriceLogs", [])
        # first non-null log = most recent
        recent = next((l for l in logs if l and l.get("Price")), None)
        if not recent:
            return {"error": "No price data"}

        price = recent["Price"]
        qty = recent.get("Quantity", 0)
        text = data.get("Text", item.get("type", "?"))
        web_url = f"https://poe2scout.com/economy/currency?search={urllib.parse.quote(text)}"

        # Build 'prices' compatible with show_result (1 row: average price)
        prices = [{"amount": round(price, 2), "currency": "exalted",
                   "account": f"vol {qty}/day", "stock": None}]
        result = {"prices": prices, "total": qty, "url": web_url,
                  "scout": True, "market_ratio": round(price, 2),
                  "price_logs": logs[:8]}
        _search_cache[ck] = (_time.time(), result)
        return result
    except Exception as e:
        return {"error": str(e)}


def search_exchange(item: dict, league: str, options: dict | None = None) -> dict:
    """Search the Currency Exchange (direct in-game buy)."""
    import urllib.parse
    options = options or {}

    want_id = _find_exchange_id(item.get("type") or item.get("text", ""))
    if not want_id:
        return {"error": f"No exchange ID for '{item.get('type', '?')}'"}

    # We pay with exalted by default
    have_id = "exalted"

    query = {
        "query": {
            "status": {"option": options.get("status", "online")},
            "have": [have_id],
            "want": [want_id],
        },
        "sort": {"have": "asc"},
        "engine": "new",
    }

    # Cache
    ck = json.dumps({"exchange": True, "want": want_id, "have": have_id,
                     "status": options.get("status", "online")}, sort_keys=True)
    cached = _search_cache.get(ck)
    if cached:
        ts, result = cached
        if _time.time() - ts < _CACHE_TTL:
            return result

    try:
        r, err = _ggg_request("POST", EXCHANGE_URL.format(league=league),
                              json=query, headers=HEADERS)
        if err:
            return {"error": err["msg"], "rate_limited": err.get("rate_limited", False),
                    "retry": err.get("retry")}
        data = r._json

        qid = data.get("id", "")
        result_dict = data.get("result", {})
        total = data.get("total", 0)
        url = f"https://www.pathofexile.com/trade2/exchange/poe2/{urllib.parse.quote(league)}/{qid}" if qid else None

        # Extract every offer with its ratio and stock
        all_offers = []
        for rid, rdata in result_dict.items():
            listing = rdata.get("listing", {})
            account = listing.get("account", {}).get("name", "?")
            for offer in listing.get("offers", [])[:1]:
                ex = offer.get("exchange", {})
                it = offer.get("item", {})
                pay = ex.get("amount", 0)
                get = it.get("amount", 1)
                stock = it.get("stock", 0)
                ratio = round(pay / get, 1) if get else 0
                all_offers.append({
                    "amount": ratio,
                    "currency": ex.get("currency", "?"),
                    "account": account,
                    "stock": stock,
                })

        # Sort by ascending ratio, prefer higher stock
        all_offers.sort(key=lambda o: (o["amount"], -o["stock"]))

        # Compute the stock-weighted market ratio (like the game does)
        market_ratio = None
        if all_offers:
            total_stock = sum(o["stock"] for o in all_offers)
            if total_stock > 0:
                weighted = sum(o["amount"] * o["stock"] for o in all_offers)
                market_ratio = round(weighted / total_stock, 1)

        prices = all_offers[:10]

        result = {"prices": prices, "total": total, "url": url,
                  "exchange": True, "market_ratio": market_ratio}
        _search_cache[ck] = (_time.time(), result)
        return result
    except Exception as e:
        return {"error": str(e)}


def get_league() -> str:
    try:
        r = requests.get(LEAGUES_URL, headers=HEADERS, timeout=6)
        leagues = r.json().get("result", [])
        for lg in leagues:
            name = lg.get("id", "")
            low = name.lower()
            if name and "standard" not in low and "hardcore" not in low:
                return name
        return leagues[0]["id"] if leagues else "Standard"
    except Exception:
        return "Standard"


def get_leagues() -> list[str]:
    """All available league ids, for the Settings league selector."""
    try:
        r = requests.get(LEAGUES_URL, headers=HEADERS, timeout=6)
        return [lg["id"] for lg in r.json().get("result", []) if lg.get("id")]
    except Exception:
        return []


_divine_price = {}   # {league: (timestamp, price_in_exalted or None)}

def get_divine_price(league: str):
    """Current Divine Orb value in exalted (from poe2scout), for ex->div display."""
    import urllib.parse
    cached = _divine_price.get(league)
    if cached and _time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    price = None
    try:
        url = SCOUT_CURR_URL.format(realm=SCOUT_REALM,
                                    league=urllib.parse.quote(league), apiid="divine")
        r = requests.get(url, headers={"User-Agent": f"{APP_ID}/{VERSION}"}, timeout=8)
        if r.status_code == 200:
            logs = r.json().get("PriceLogs", [])
            recent = next((l for l in logs if l and l.get("Price")), None)
            if recent:
                price = float(recent["Price"])
    except Exception:
        price = None
    _divine_price[league] = (_time.time(), price)
    return price


def _div_hint(amount, currency, divine):
    """Return a '≈X div' string when an exalted price is worth >= ~1 divine."""
    try:
        if divine and divine > 0 and currency in ("exalted", "ex") and float(amount) >= divine:
            return f"≈{_fmt(round(float(amount) / divine, 1))} div"
    except Exception:
        pass
    return None


def _build_query(item, stats, options=None):
    options = options or {}
    # The "status" option doubles as the market selector in PoE2:
    #   securable = Instant Buyout marketplace (where ~99% of listings are)
    #   available = instant buyout + in-person, online/onlineleague = whisper
    #   sellers only, any = everything. Default to the instant-buyout market.
    query = {"status": {"option": options.get("status", "securable")}}
    if item["unique"] and item["name"]:
        query["name"] = item["name"]
        query["type"] = item["type"]
        query["filters"] = {"type_filters": {"filters": {"rarity": {"option": "unique"}}}}
    else:
        query["type"] = item["type"] or item["text"]

    pmax = options.get("price_max")
    if pmax:
        query.setdefault("filters", {})["trade_filters"] = {
            "filters": {"price": {"max": pmax}}}

    # Corrupted filter: "true"/"false" restricts; anything else = any
    corr = options.get("corrupted")
    if corr in ("true", "false"):
        query.setdefault("filters", {})["misc_filters"] = {
            "filters": {"corrupted": {"option": corr}}}

    # Split into stat-mod filters (have a stat 'id') and equipment filters
    # (DPS/defenses, keyed by an 'equip' id -> trade 'equipment_filters').
    stats = stats or []
    mod_filters, equip_vals = [], {}
    for s in stats:
        mn = s.get("min", s.get("value"))
        mx = s.get("max")
        val = {}
        if mn is not None:
            val["min"] = round(mn, 2)
        if mx is not None:
            val["max"] = round(mx, 2)
        if s.get("equip"):
            if val:
                equip_vals[s["equip"]] = val
        elif s.get("id"):
            f = {"id": s["id"], "disabled": False}
            if val:
                f["value"] = val
            mod_filters.append(f)

    if equip_vals:
        query.setdefault("filters", {})["equipment_filters"] = {"filters": equip_vals}
    if mod_filters:
        query["stats"] = [{"type": "and", "filters": mod_filters}]
    return query


import time as _time
from collections import deque

_search_cache = {}        # {cache_key: (timestamp, result)}
_CACHE_TTL = 600          # 10-minute cache

# ── Rate limiter ──────────────────────────────────────────────────────────────
# The GGG trade API publishes its limits in headers, e.g.
#   X-Rate-Limit-Ip: 5:10:60,15:60:300,30:300:1800
# meaning: max 5 req / 10s (else 60s ban), 15/60s (300s ban), 30/300s (1800s ban).
# We track our own request timestamps and wait *before* a request so we never
# trip a rule. If we still get banned, we record it and report it to the UI
# instead of pretending there were no results.

_req_times = deque()                       # timestamps of recent GGG API calls
_rate_rules = [(5, 10), (15, 60), (30, 300)]   # (max_hits, period_s); updated from headers
_banned_until = 0.0


def _ban_remaining() -> int:
    rem = _banned_until - _time.time()
    return int(rem) + 1 if rem > 0 else 0


def _rl_wait():
    """Block until making a request keeps us within every rate rule."""
    while True:
        now = _time.time()
        while _req_times and now - _req_times[0] > _rate_rules[-1][1]:
            _req_times.popleft()
        wait = 0.0
        for mx, period in _rate_rules:
            recent = sum(1 for t in _req_times if now - t < period)
            if recent >= mx:
                oldest = next(t for t in _req_times if now - t < period)
                wait = max(wait, period - (now - oldest) + 0.2)
        if wait <= 0:
            break
        _time.sleep(min(wait, 5.0))
    _req_times.append(_time.time())


def _update_rules(resp):
    """Adopt the server's advertised limits if present."""
    global _rate_rules
    hdr = resp.headers.get("X-Rate-Limit-Ip")
    if not hdr:
        return
    try:
        rules = []
        for part in hdr.split(","):
            mx, period, _ban = part.split(":")
            rules.append((int(mx), int(period)))
        if rules:
            _rate_rules = rules
    except Exception:
        pass


def _retry_from_msg(msg: str) -> int:
    m = re.search(r"(\d+)\s*second", msg or "")
    return int(m.group(1)) if m else 60


def _ggg_request(method, url, **kw):
    """
    Throttled GGG API request. Returns (response, err) where err is None or a
    dict {"msg", "rate_limited", "retry"}. Never raises for rate limits.
    """
    global _banned_until
    ban = _ban_remaining()
    if ban:
        return None, {"msg": f"Rate limited — wait {ban}s",
                      "rate_limited": True, "retry": ban}
    _rl_wait()
    r = requests.request(method, url, timeout=10, **kw)
    _update_rules(r)
    if r.status_code == 429:
        retry = int(r.headers.get("Retry-After", _retry_from_msg(r.text)))
        _banned_until = _time.time() + retry
        return None, {"msg": f"Rate limited — wait {retry}s",
                      "rate_limited": True, "retry": retry}
    try:
        data = r.json()
    except Exception:
        return None, {"msg": f"HTTP {r.status_code}", "rate_limited": False}
    # Some rate-limit errors come back as 200 + an error body
    if isinstance(data, dict) and "error" in data:
        msg = data["error"].get("message", "API error")
        if "rate limit" in msg.lower():
            retry = _retry_from_msg(msg)
            _banned_until = _time.time() + retry
            return None, {"msg": f"Rate limited — wait {retry}s",
                          "rate_limited": True, "retry": retry}
        return None, {"msg": msg, "rate_limited": False}
    r._json = data
    return r, None


def _do_search(query, league):
    body = {"query": query, "sort": {"price": "asc"}}
    r, err = _ggg_request("POST", SEARCH_URL.format(league=league), json=body, headers=HEADERS)
    if err:
        return None, err
    return r._json, None


def search_trade(item: dict, league: str, stats: list[dict] | None = None,
                 exact: bool = False, options: dict | None = None) -> dict:
    """
    Search listings. If exact=True, respects the filters as-is.
    10-minute cache so we don't hammer the API.
    """
    import urllib.parse
    stats = stats or []

    # Cache key: item + stats + options
    ck = json.dumps({"t": item.get("type"), "n": item.get("name"),
                     "u": item.get("unique"), "s": stats, "o": options,
                     "e": exact}, sort_keys=True)
    cached = _search_cache.get(ck)
    if cached:
        ts, result = cached
        if _time.time() - ts < _CACHE_TTL:
            return result
    if exact:
        plans = [stats]
    else:
        plans, seen = [], set()
        for k in (len(stats), 2, 0):
            k = max(0, min(k, len(stats)))
            if k not in seen:
                seen.add(k); plans.append(stats[:k])

    try:
        data, used = None, []
        for used in plans:
            data, err = _do_search(_build_query(item, used, options), league)
            if err:
                return {"error": err["msg"], "rate_limited": err.get("rate_limited", False),
                        "retry": err.get("retry")}
            if data.get("result"):
                break

        ids = data.get("result", [])[:10]
        qid = data.get("id", "")
        total = data.get("total", len(data.get("result", [])))
        url = WEB_URL.format(league=urllib.parse.quote(league), qid=qid) if qid else None
        if not ids:
            return {"prices": [], "total": 0, "url": url, "stats_used": len(used)}

        r2, err = _ggg_request("GET", FETCH_URL.format(ids=",".join(ids), qid=qid),
                               headers=HEADERS)
        if err:
            return {"error": err["msg"], "rate_limited": err.get("rate_limited", False),
                    "retry": err.get("retry")}
        prices = []
        for entry in r2._json.get("result", []):
            lst = (entry or {}).get("listing", {})
            price = lst.get("price") or {}
            if price.get("amount"):
                # price.type: "~b/o" = firm buyout, "~price" = asking price.
                # Listings with a "fee" come from the instant-buyout market.
                ptype = (price.get("type") or "").replace("~", "")
                prices.append({
                    "amount": price["amount"],
                    "currency": price.get("currency", "?"),
                    "account": lst.get("account", {}).get("name", "?"),
                    "ptype": ptype,
                    "instant": "fee" in lst,
                    "demand": bool(lst.get("in_demand")),
                })
        result = {"prices": prices, "total": total, "url": url, "stats_used": len(used)}
        _search_cache[ck] = (_time.time(), result)
        return result
    except Exception as e:
        return {"error": str(e)}


# ── Pseudo mods + filter building (Awakened-style) ────────────────────────────

def _find_pseudo(stat_db, needle):
    for s in stat_db:
        if s.get("group") == "Pseudo" and needle in s["text"].lower():
            return s["id"], s["text"]
    return None, None


def compute_equipment_filters(lines):
    """
    Compute the base-property filters that matter for weapons/armour:
    DPS (physical/elemental/total), crit, attack speed, and defenses
    (armour/evasion/energy shield/ward/spirit). Maps to the trade API's
    'equipment_filters'. Returns filter dicts (each carries an 'equip' id).
    """
    # Match WITHIN a single line (never across newlines, which would e.g. grab
    # the "143" of "143% increased…" as Spirit from a "+15 to Spirit" line).
    def rng(kw):   # average of an "A-B" range on a single line, else 0
        for ln in lines:
            m = re.search(kw + r"\s*:?\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", ln, re.I)
            if m:
                return (float(m.group(1)) + float(m.group(2))) / 2.0
        return 0.0

    def num(kw):
        for ln in lines:
            m = re.search(kw + r"\s*:?\s*(\d+(?:\.\d+)?)", ln, re.I)
            if m:
                return float(m.group(1))
        return 0.0

    aps = num(r"attacks per second")
    phys = rng(r"physical damage")
    ele = rng(r"fire damage") + rng(r"cold damage") + rng(r"lightning damage")
    chaos = rng(r"chaos damage")
    crit = num(r"critical hit chance")
    ar = num(r"armou?r"); ev = num(r"evasion rating"); es = num(r"energy shield")
    ward = num(r"runic ward"); spirit = num(r"spirit")

    rows = []   # (equip_id, label, value, enabled_by_default)
    if aps > 0 and (phys or ele or chaos):
        pdps = round(phys * aps, 1)
        edps = round(ele * aps, 1)
        dps = round((phys + ele + chaos) * aps, 1)
        if dps > 0:  rows.append(("dps", "Total DPS", dps, True))
        if pdps > 0: rows.append(("pdps", "Physical DPS", pdps, False))
        if edps > 0: rows.append(("edps", "Elemental DPS", edps, False))
        if crit > 0: rows.append(("crit", "Crit Chance", crit, False))
        if aps > 0:  rows.append(("aps", "Attacks/sec", aps, False))
    for eid, label, v in (("ar", "Armour", ar), ("ev", "Evasion", ev),
                          ("es", "Energy Shield", es), ("ward", "Ward", ward),
                          ("spirit", "Spirit", spirit)):
        if v > 0:
            rows.append((eid, label, v, True))

    out = []
    for i, (eid, label, v, en) in enumerate(rows):
        out.append({
            "equip": eid, "text": f"{label}: {v:g}", "value": v, "pseudo": False,
            "enabled": en, "min_default": round(v * 0.9, 2),
            "section": "Damage" if eid in ("dps", "pdps", "edps", "crit", "aps") else "Defenses",
            "sort": (-2, i),
        })
    return out


def compute_pseudos(matched, stat_db):
    """Combine related mods into pseudo-stats (total elemental res, life...)."""
    fire = cold = light = chaos = life = mana = 0.0
    for m in matched:
        t = m["text"].lower(); v = m["value"] or 0
        if "all elemental resistances" in t: fire += v; cold += v; light += v
        elif "fire and cold" in t: fire += v; cold += v
        elif "fire and lightning" in t: fire += v; light += v
        elif "cold and lightning" in t: cold += v; light += v
        elif "fire and chaos" in t: fire += v; chaos += v
        elif "cold and chaos" in t: cold += v; chaos += v
        elif "lightning and chaos" in t: light += v; chaos += v
        elif "to fire resistance" in t: fire += v
        elif "to cold resistance" in t: cold += v
        elif "to lightning resistance" in t: light += v
        elif "to chaos resistance" in t: chaos += v
        if "to maximum life" in t: life += v
        if "to maximum mana" in t: mana += v

    ele = fire + cold + light
    out = []
    for needle, val in [("total elemental resistance", ele),
                        ("total resistance", ele + chaos),
                        ("to maximum life", life),
                        ("to maximum mana", mana)]:
        if val <= 0:
            continue
        sid, stext = _find_pseudo(stat_db, needle)
        if sid:
            out.append({"id": sid, "text": stext, "value": val, "pseudo": True})
    return out


# Prefix/suffix heuristic for explicit mods (no public affix DB needed; covers
# the common cases so the list reads like the game: prefixes first, suffixes
# after). Unknown mods fall into "Other mods" at the end.
_SUFFIX_RX = re.compile(
    r"resistance|to (strength|dexterity|intelligence|all attributes)"
    r"|accuracy|attack speed|cast speed|critical|leech"
    r"|light radius|stun|thorns|rarity of items"
    r"|chance to (shock|ignite|freeze|chill|poison|blind|bleed)"
    r"|reduced attribute|on kill|flask charges", re.I)
_PREFIX_RX = re.compile(
    r"adds\s+[#\d]+\s+to\s+[#\d]+|increased .{0,24}damage|to maximum (life|mana|energy shield)"
    r"|increased (armour|evasion|energy shield|spirit)"
    r"|to (armour|evasion rating|energy shield|spirit)"
    r"|to level of|projectile|area of effect|charm slot|flask life recovery", re.I)


def _affix_kind(text: str):
    if _SUFFIX_RX.search(text):
        return "suffix"
    if _PREFIX_RX.search(text):
        return "prefix"
    return None


def build_filters(item, matched, stat_db, equip=None):
    """
    Editable filters grouped like in-game.
      - Damage/Defenses: weapon DPS & armour values (equipment_filters), enabled.
      - Rare: Combined pseudos (enabled) + Implicit + Modifiers.
      - Unique: its affixes ENABLED by default with the item's current roll as
        the minimum, so the price reflects the roll. Most uniques share the same
        mod set and only the range varies; a high roll can be worth far more
        (1 div vs 1 ex), so we filter by roll from the start.
    Each filter carries 'section' (header), 'enabled' and 'min_default'.
    """
    is_u = item["unique"]
    filters = list(equip or [])   # Damage/Defenses go first (sort -2)

    if not is_u:
        for ps in compute_pseudos(matched, stat_db):
            val = ps.get("value")
            filters.append({**ps, "enabled": True, "section": "Combined",
                            "min_default": int(val * 0.9) if val else None,
                            "sort": (-1, 0)})

    for i, m in enumerate(matched):
        grp = m.get("group", "Explicit")
        # Explicit mods read like the game: Prefixes first, then Suffixes,
        # with anything unclassified at the end under "Other mods".
        if grp == "Implicit":
            section, rank = "Implicit", 0
        elif grp == "Enchant":
            section, rank = "Enchant", 1
        else:
            kind = _affix_kind(m["text"])
            section, rank = {"prefix": ("Prefixes", 2),
                             "suffix": ("Suffixes", 3)}.get(kind, ("Other mods", 4))
        val = m["value"]
        # Uniques: pre-enabled, exact roll as minimum. Rares: disabled, 90% min.
        filters.append({
            "id": m["id"], "text": m["text"], "value": val, "pseudo": False,
            "enabled": bool(is_u and val is not None),
            "min_default": (int(val) if (is_u and val is not None)
                            else int(val * 0.9) if val is not None else None),
            "section": section,
            "sort": (rank, i),
        })

    filters.sort(key=lambda f: f["sort"])
    # Rares with no pseudos: enable the first 2 mods so the initial search helps.
    if not is_u and not any(f["enabled"] for f in filters):
        for f in filters[:2]:
            f["enabled"] = True
    return filters


# ── Overlay UI (interactive price-check window) ───────────────────────────────

BG, CARD, PANEL = "#0b0b0d", "#17140f", "#121110"
GOLD, GOLD2, WHITE = "#c79a4b", "#e8c879", "#d6d2c8"
MUTE, LINE = "#6f6a62", "#2a2620"
UNIQUE_C, RARE_C, MAGIC_C, NORMAL_C = "#af6025", "#e5d54a", "#8a8aff", "#c8c8c8"
CORRUPT, GREEN, BLUE = "#d24b3a", "#86d98a", "#5a8fcf"
PSEUDO_C = "#9fb8ff"

RARITY_COLOR = {"unique": UNIQUE_C, "rare": RARE_C, "magic": MAGIC_C, "normal": NORMAL_C}


def _round_up(x, step):
    import math
    return int(math.ceil(x / step) * step)


def slider_domain(text, value):
    """Slider [lo, hi] domain based on the mod type and the item's roll."""
    if value is None:
        return None
    t = text.lower()
    if "resistance" in t and "total" not in t:
        hi = max(50, _round_up(value * 1.25, 5))      # single res ~ caps at 48-50
    elif "total" in t and "resist" in t:
        hi = _round_up(value * 1.4, 5)
    elif "%" in text or "increased" in t or "reduced" in t or "more" in t:
        hi = _round_up(value * 1.6, 5)
    else:
        hi = _round_up(value * 1.6, 1)
    return 0, max(hi, _round_up(value * 1.1, 1))


class RangeSlider(tk.Canvas):
    """Dual-handle (min / max) range slider, PoE Overlay style."""
    def __init__(self, master, lo, hi, vmin, vmax, width=168, height=26):
        super().__init__(master, width=width, height=height, bg=PANEL,
                         highlightthickness=0, cursor="hand2")
        self.lo, self.hi = float(lo), float(hi if hi > lo else lo + 1)
        self.vmin = max(self.lo, min(float(vmin), self.hi))
        self.vmax = max(self.vmin, min(float(vmax), self.hi))
        self.W, self.H, self.pad = width, height, 10
        self._drag = None
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self._draw()

    def _x(self, v):
        return self.pad + (v - self.lo) / (self.hi - self.lo) * (self.W - 2 * self.pad)

    def _v(self, x):
        f = (x - self.pad) / (self.W - 2 * self.pad)
        return self.lo + max(0.0, min(1.0, f)) * (self.hi - self.lo)

    def _draw(self):
        self.delete("all")
        y = self.H // 2 + 4
        self.create_line(self.pad, y, self.W - self.pad, y, fill="#3a342a", width=3)
        x1, x2 = self._x(self.vmin), self._x(self.vmax)
        self.create_line(x1, y, x2, y, fill=GOLD, width=3)
        for x in (x1, x2):
            self.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#ffd9a0", outline=GOLD)
        mn = _fmt(round(self.vmin)) if self.vmin > self.lo else "0"
        mx = "max" if self.vmax >= self.hi else _fmt(round(self.vmax))
        self.create_text(self.W // 2, 7, text=f"{mn} – {mx}",
                         fill=WHITE, font=("Consolas", 7))

    def _press(self, e):
        self._drag = "min" if abs(e.x - self._x(self.vmin)) <= abs(e.x - self._x(self.vmax)) else "max"
        self._move(e)

    def _move(self, e):
        v = round(self._v(e.x))
        if self._drag == "min":
            self.vmin = min(v, self.vmax)
        else:
            self.vmax = max(v, self.vmin)
        self._draw()

    def get(self):
        mn = self.vmin if self.vmin > self.lo else None
        mx = self.vmax if self.vmax < self.hi else None
        return mn, mx


class Overlay:
    def __init__(self):
        self._ready = threading.Event()
        self._url = None
        self._on_search = None
        self._fw = []              # [(BooleanVar, RangeSlider, filter)]
        self.divine_price = None   # Divine value in exalted, for ex->div hints
        self._history = deque(maxlen=6)   # recent checks: {name,rarity,summary,url}
        self._last_name = None     # last shown item (for the history)
        self._last_rarity = "rare"
        self.leagues = []          # available league ids (for Settings)
        self.on_settings_saved = None    # main() hooks this to apply live
        self._cfg = None           # live config dict (set by main)
        self._t0 = _time.time()    # start time, for the pill's uptime
        self._pill_league = None
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait()

    def _run(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)          # no Windows title bar -> cleaner
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.configure(bg=BG)
        self.root.geometry("520x900+24+24")

        FN = "Segoe UI"
        # gold-bordered frame
        outer = tk.Frame(self.root, bg=GOLD); outer.pack(fill="both", expand=True)
        wrap = tk.Frame(outer, bg=BG); wrap.pack(fill="both", expand=True, padx=1, pady=1)

        # --- title bar (draggable + close) ---
        title = tk.Frame(wrap, bg="#0f0e0c"); title.pack(fill="x")
        title.bind("<ButtonPress-1>", self._ds); title.bind("<B1-Motion>", self._dm)
        tk.Label(title, text=f"◈  {APP_NAME}", bg="#0f0e0c", fg=GOLD2,
                 font=(FN, 10, "bold")).pack(side="left", padx=(10, 2), pady=5)
        tk.Label(title, text=f"v{VERSION}", bg="#0f0e0c", fg=MUTE,
                 font=(FN, 8)).pack(side="left", pady=5)
        tk.Button(title, text="✕", command=self.quit_app, bg="#0f0e0c", fg=MUTE,
                  font=(FN, 9), relief="flat", cursor="hand2", bd=0,
                  activebackground="#0f0e0c", activeforeground=CORRUPT).pack(side="right", padx=6)
        tk.Button(title, text="—", command=self.sleep, bg="#0f0e0c", fg=MUTE,
                  font=(FN, 9), relief="flat", cursor="hand2", bd=0,
                  activebackground="#0f0e0c", activeforeground=GOLD2).pack(side="right")
        tk.Button(title, text="⚙", command=self.open_settings, bg="#0f0e0c", fg=MUTE,
                  font=(FN, 9), relief="flat", cursor="hand2", bd=0,
                  activebackground="#0f0e0c", activeforeground=GOLD2).pack(side="right")
        self.hint_lbl = tk.Label(title, text="F5 check · ESC hide", bg="#0f0e0c",
                                 fg=MUTE, font=(FN, 8))
        self.hint_lbl.pack(side="right", padx=8)

        # --- item card (gold strip + name + organized properties) ---
        card = tk.Frame(wrap, bg=CARD); card.pack(fill="x", padx=8, pady=(8, 4))
        tk.Frame(card, bg=GOLD, width=3).pack(side="left", fill="y")
        cin = tk.Frame(card, bg=CARD); cin.pack(side="left", fill="x", expand=True, padx=10, pady=7)
        self.item_lbl = tk.Label(cin, text="Point at an item and press F5", bg=CARD,
                                 fg=GOLD2, font=(FN, 14, "bold"), anchor="w")
        self.item_lbl.pack(fill="x")
        self.base_lbl = tk.Label(cin, text="", bg=CARD, fg=MUTE, font=(FN, 9), anchor="w")
        self.base_lbl.pack(fill="x")
        # properties block (rarity / item level / defenses / requires…)
        self.props = tk.Frame(cin, bg=CARD)
        self.props.pack(fill="x", pady=(5, 0))

        # --- trade options ---
        # Market = the PoE2 trade "status" dimension. The instant-buyout
        # marketplace (status: securable) is where nearly all real listings
        # live; in-person whisper listings are a tiny dying remnant.
        opt = tk.Frame(wrap, bg=BG); opt.pack(fill="x", padx=10, pady=(2, 2))
        self.market_var = tk.StringVar(value="Instant Buyout")
        self.corr_var = tk.StringVar(value="Any")
        self._opt_menu(opt, "Market", self.market_var,
                       ["Instant Buyout", "Buyout + In Person",
                        "In Person", "Any"]).pack(side="left")
        self._opt_menu(opt, "Corrupted", self.corr_var,
                       ["Any", "No", "Yes"]).pack(side="left", padx=(10, 0))

        # --- FILTERS section ---
        self._section(wrap, "FILTERS")
        self.filters = tk.Frame(wrap, bg=BG); self.filters.pack(fill="x", padx=10)

        # --- action bar ---
        bar = tk.Frame(wrap, bg=BG); bar.pack(fill="x", padx=10, pady=(8, 2))
        self.search_btn = tk.Button(bar, text="⟳  Search", command=self._fire_search,
                                    bg=GOLD, fg="#1a1206", font=(FN, 10, "bold"),
                                    relief="flat", cursor="hand2", state="disabled",
                                    activebackground=GOLD2, bd=0, padx=14, pady=3)
        self.search_btn.pack(side="left")
        self.web_btn = tk.Button(bar, text="↗ Web", command=self.open_web,
                                 bg="#1e2c3e", fg="#cfe2ff", font=(FN, 9),
                                 relief="flat", cursor="hand2", state="disabled",
                                 bd=0, padx=10, pady=3)
        self.web_btn.pack(side="left", padx=6)
        self.summary_lbl = tk.Label(bar, text="", bg=BG, fg=GREEN, font=(FN, 16, "bold"))
        self.summary_lbl.pack(side="right")

        # --- LISTINGS section ---
        self._section(wrap, "LISTINGS")
        self.prices = tk.Frame(wrap, bg=BG); self.prices.pack(fill="both", expand=True, padx=10)

        # --- RECENT checks (compact, clickable history) ---
        self._section(wrap, "RECENT")
        self.history_frame = tk.Frame(wrap, bg=BG)
        self.history_frame.pack(fill="x", padx=10, pady=(0, 2))

        self.status = tk.Label(wrap, text="Loading…", bg="#0f0e0c", fg=MUTE,
                               font=(FN, 8), anchor="w")
        self.status.pack(fill="x", side="bottom", ipady=2)

        # RIGHT-click anywhere -> hide (doesn't interfere with the controls)
        self.root.bind_all("<Button-3>", lambda e: self.sleep())
        # Auto-hide: when the cursor is outside and the user left-clicks.
        self._mouse_inside = True
        self.root.bind("<Enter>", lambda e: setattr(self, '_mouse_inside', True))
        self.root.bind("<Leave>", lambda e: setattr(self, '_mouse_inside', False))

        def _poll_autohide():
            if self.root.winfo_ismapped() and not self._mouse_inside:
                state = ctypes.windll.user32.GetAsyncKeyState(0x01)  # VK_LBUTTON
                if state & 0x8000:
                    self.root.withdraw()   # direct, no ev.wait (we're in mainloop)
                    self._pill_show_now()
            self.root.after(150, _poll_autohide)
        self.root.after(500, _poll_autohide)

        # --- status pill: tiny draggable badge shown while the overlay sleeps,
        # so users always see the app is running. Click = open overlay;
        # right-click = hide the pill for this session. ---
        self.pill = tk.Toplevel(self.root)
        self.pill.overrideredirect(True)
        self.pill.attributes("-topmost", True)
        self.pill.attributes("-alpha", 0.88)
        pf = tk.Frame(self.pill, bg=GOLD); pf.pack(fill="both", expand=True)
        self.pill_lbl = tk.Label(pf, text=f"◈ {APP_NAME}", bg="#0f0e0c", fg=GOLD2,
                                 font=(FN, 8, "bold"), padx=8, pady=2, cursor="hand2")
        self.pill_lbl.pack(padx=1, pady=1)
        self.pill_lbl.bind("<ButtonPress-1>", self._pill_press)
        self.pill_lbl.bind("<B1-Motion>", self._pill_drag)
        self.pill_lbl.bind("<ButtonRelease-1>", self._pill_release)
        self.pill_lbl.bind("<Button-3>", lambda e: self.pill.withdraw())
        self.pill.withdraw()
        self.root.after(1000, self._pill_tick)

        self._tk_thread = threading.current_thread()
        self.root.withdraw()           # starts ASLEEP (hidden). F5 wakes it.
        self._ready.set()
        self.root.mainloop()

    # --- status pill helpers ---
    def _pill_text(self):
        up = int(_time.time() - self._t0)
        h, m = up // 3600, (up % 3600) // 60
        lg = f" · {self._pill_league}" if self._pill_league else ""
        return f"◈ {APP_NAME}{lg} · v{VERSION} · {h}:{m:02d}"

    def _pill_tick(self):
        self.pill_lbl.config(text=self._pill_text())
        self.root.after(30000, self._pill_tick)

    def _pill_show_now(self):
        """tk-thread only: show the pill (if enabled in config)."""
        if not (self._cfg or {}).get("pill", True):
            return
        self.pill_lbl.config(text=self._pill_text())
        pos = (self._cfg or {}).get("pill_pos")
        if pos:
            self.pill.geometry(f"+{int(pos[0])}+{int(pos[1])}")
        else:
            self.pill.update_idletasks()
            self.pill.geometry(f"+{self.pill.winfo_screenwidth()//2 - 110}+4")
        self.pill.deiconify()
        self.pill.attributes("-topmost", True)

    def pill_set(self, league):
        """Set the league shown on the pill and refresh it (any thread)."""
        self._pill_league = league
        self.root.after(0, lambda: (self.pill_lbl.config(text=self._pill_text()),
                                    self.root.winfo_ismapped() or self._pill_show_now()))

    def _pill_press(self, e):
        self._pdx, self._pdy = e.x_root - self.pill.winfo_x(), e.y_root - self.pill.winfo_y()
        self._pmoved = False

    def _pill_drag(self, e):
        self._pmoved = True
        self.pill.geometry(f"+{e.x_root - self._pdx}+{e.y_root - self._pdy}")

    def _pill_release(self, e):
        if self._pmoved:
            if self._cfg is not None:   # remember where the user parked it
                self._cfg["pill_pos"] = [self.pill.winfo_x(), self.pill.winfo_y()]
                save_config(self._cfg)
        else:
            self.wake()                 # plain click -> open the overlay

    def _section(self, parent, text):
        f = tk.Frame(parent, bg=BG); f.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(f, text=text, bg=BG, fg=GOLD, font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Frame(f, bg=LINE, height=1).pack(side="left", fill="x", expand=True, padx=(8, 0), pady=6)

    def _opt_menu(self, parent, label, var, options):
        box = tk.Frame(parent, bg=BG)
        tk.Label(box, text=label, bg=BG, fg=MUTE, font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        m = tk.OptionMenu(box, var, *options)
        m.config(bg=CARD, fg=WHITE, font=("Segoe UI", 8), relief="flat", bd=0,
                 highlightthickness=0, activebackground=GOLD, cursor="hand2",
                 indicatoron=True, padx=6, pady=1)
        m["menu"].config(bg=CARD, fg=WHITE, font=("Segoe UI", 8), activebackground=GOLD)
        m.pack(side="left")
        return box

    # --- draggable window ---
    def _ds(self, e): self._x, self._y = e.x, e.y
    def _dm(self, e):
        self.root.geometry(f"+{self.root.winfo_x()+e.x-self._x}+{self.root.winfo_y()+e.y-self._y}")

    def open_web(self):
        if self._url:
            webbrowser.open(self._url)

    def open_config(self):
        """Open the editable config.json in the default editor."""
        try:
            if not os.path.exists(CONFIG_PATH):
                save_config(load_config())
            os.startfile(CONFIG_PATH)   # Windows: opens with default app
            self.set_status("Edit config.json, then restart to apply.", BLUE)
        except Exception as e:
            self.set_status(f"Couldn't open config: {e}", "#f80")

    def open_settings(self):
        """In-app Settings window: league, hotkeys, behavior. Applies live."""
        def u():
            if getattr(self, "_swin", None) and self._swin.winfo_exists():
                self._swin.lift(); return
            cfg = load_config()
            win = tk.Toplevel(self.root)
            self._swin = win
            win.title("Settings")
            win.configure(bg=BG)
            win.attributes("-topmost", True)
            win.geometry(f"+{self.root.winfo_x()+60}+{self.root.winfo_y()+60}")
            body = tk.Frame(win, bg=BG); body.pack(fill="both", expand=True, padx=14, pady=10)

            tk.Label(body, text="SETTINGS", bg=BG, fg=GOLD,
                     font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                         sticky="w", pady=(0, 8))
            # League selector (live list; "(auto)" = detect current league)
            tk.Label(body, text="League", bg=BG, fg=MUTE,
                     font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=2)
            lg_values = ["(auto)"] + (self.leagues or [])
            cur = cfg.get("league") or "(auto)"
            if cur not in lg_values:
                lg_values.append(cur)
            lg_var = tk.StringVar(value=cur)
            lg_menu = tk.OptionMenu(body, lg_var, *lg_values)
            lg_menu.config(bg=CARD, fg=WHITE, font=("Segoe UI", 9), relief="flat",
                           bd=0, highlightthickness=0, activebackground=GOLD)
            lg_menu["menu"].config(bg=CARD, fg=WHITE, activebackground=GOLD)
            lg_menu.grid(row=1, column=1, sticky="ew", pady=2)

            # Hotkey entries
            hk = cfg.get("hotkeys", dict(_DEFAULT_CONFIG["hotkeys"]))
            hk_vars = {}
            labels = [("check", "Price check"), ("web", "Open web"),
                      ("refresh", "Refresh DBs"), ("hide", "Hide overlay"),
                      ("quit", "Quit")]
            for i, (key, lbl) in enumerate(labels, start=2):
                tk.Label(body, text=lbl, bg=BG, fg=MUTE,
                         font=("Segoe UI", 9)).grid(row=i, column=0, sticky="w", pady=2)
                v = tk.StringVar(value=hk.get(key, _DEFAULT_CONFIG["hotkeys"][key]))
                hk_vars[key] = v
                tk.Entry(body, textvariable=v, bg=CARD, fg=WHITE, relief="flat",
                         insertbackground=WHITE, font=("Consolas", 9),
                         width=18).grid(row=i, column=1, sticky="ew", pady=2)

            near_var = tk.BooleanVar(value=bool(cfg.get("near_cursor", True)))
            tk.Checkbutton(body, text="Open overlay next to the cursor",
                           variable=near_var, bg=BG, fg=WHITE, selectcolor=CARD,
                           activebackground=BG, activeforeground=WHITE,
                           font=("Segoe UI", 9), highlightthickness=0
                           ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 2))
            pill_var = tk.BooleanVar(value=bool(cfg.get("pill", True)))
            tk.Checkbutton(body, text="Show status pill while hidden",
                           variable=pill_var, bg=BG, fg=WHITE, selectcolor=CARD,
                           activebackground=BG, activeforeground=WHITE,
                           font=("Segoe UI", 9), highlightthickness=0
                           ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, 2))

            msg = tk.Label(body, text="", bg=BG, fg=GREEN, font=("Segoe UI", 8))
            msg.grid(row=9, column=0, columnspan=2, sticky="w")

            def do_save():
                new = {**cfg,
                       "league": None if lg_var.get() == "(auto)" else lg_var.get(),
                       "near_cursor": bool(near_var.get()),
                       "pill": bool(pill_var.get()),
                       "hotkeys": {k: v.get().strip() or _DEFAULT_CONFIG["hotkeys"][k]
                                   for k, v in hk_vars.items()}}
                save_config(new)
                msg.config(text="Saved — applying…")
                if self.on_settings_saved:
                    threading.Thread(target=self.on_settings_saved, args=(new,),
                                     daemon=True).start()
                win.after(700, win.destroy)

            btns = tk.Frame(body, bg=BG); btns.grid(row=10, column=0, columnspan=2,
                                                    sticky="ew", pady=(10, 0))
            tk.Button(btns, text="Save & apply", command=do_save, bg=GOLD,
                      fg="#1a1206", font=("Segoe UI", 9, "bold"), relief="flat",
                      cursor="hand2", bd=0, padx=12, pady=3).pack(side="left")
            tk.Button(btns, text="Edit raw JSON", command=self.open_config,
                      bg=CARD, fg=MUTE, font=("Segoe UI", 8), relief="flat",
                      cursor="hand2", bd=0, padx=10, pady=3).pack(side="left", padx=8)
            tk.Button(btns, text="Close", command=win.destroy, bg=CARD, fg=MUTE,
                      font=("Segoe UI", 8), relief="flat", cursor="hand2", bd=0,
                      padx=10, pady=3).pack(side="right")
            body.columnconfigure(1, weight=1)
        self.root.after(0, u)

    def sleep(self, show_pill=True):
        """Fully hide the window (sleep mode). Keeps listening for F5.
        show_pill=False is used during capture so the pill isn't in the shot."""
        def _do():
            self.root.withdraw()
            if show_pill:
                self._pill_show_now()
            else:
                self.pill.withdraw()
        try:
            # On the tkinter thread: run directly (after+wait would deadlock).
            if threading.current_thread() is getattr(self, "_tk_thread", None):
                _do()
            else:
                ev = threading.Event()
                self.root.after(0, lambda: (_do(), ev.set()))
                ev.wait()
        except Exception:
            pass

    def wake(self):
        """Show the window (after the capture) without stealing game focus."""
        def u():
            self.pill.withdraw()
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
        self.root.after(0, u)

    def quit_app(self):
        os._exit(0)

    def set_status(self, msg, color=MUTE):
        self.root.after(0, lambda: self.status.config(text=msg, fg=color))

    def active_options(self):
        # Market -> trade API "status" option:
        #   securable = Instant Buyout (the real marketplace; default)
        #   available = Instant Buyout + In Person
        #   online    = In Person (whisper sellers only)
        #   any       = everything, including offline
        st = {"Instant Buyout": "securable", "Buyout + In Person": "available",
              "In Person": "online", "Any": "any"}.get(
                  self.market_var.get(), "securable")
        corr = {"Any": None, "No": "false", "Yes": "true"}.get(self.corr_var.get())
        opts = {"status": st}
        if corr:
            opts["corrupted"] = corr
        return opts

    def show_item(self, name, base, corrupted, rarity="rare", props=None, dps=None):
        """Trade-website style item card: one labeled property per line on the
        left, with the computed DPS block top-right in blue."""
        props = props or []
        dps = dps or []
        self._last_name = name
        self._last_rarity = rarity
        def u():
            color = CORRUPT if corrupted else RARITY_COLOR.get(rarity, RARE_C)
            self.item_lbl.config(text=name or "Unknown item", fg=color)
            # base type + rarity label
            rar = {"unique": "Unique", "rare": "Rare", "magic": "Magic",
                   "normal": "Normal"}.get(rarity, "")
            sub = base + (f"   ·   {rar}" if rar else "")
            if corrupted:
                sub += "   ·   CORRUPTED"
            self.base_lbl.config(text=sub, fg=CORRUPT if corrupted else MUTE)

            for w in self.props.winfo_children(): w.destroy()
            cols = tk.Frame(self.props, bg=CARD); cols.pack(fill="x")
            left = tk.Frame(cols, bg=CARD)
            left.pack(side="left", fill="x", expand=True, anchor="nw")
            right = tk.Frame(cols, bg=CARD)
            right.pack(side="right", anchor="ne", padx=(8, 0))

            for label, value in props:
                row = tk.Frame(left, bg=CARD); row.pack(fill="x")
                tk.Label(row, text=f"{label}: ", bg=CARD, fg=MUTE,
                         font=("Segoe UI", 9)).pack(side="left")
                tk.Label(row, text=value, bg=CARD,
                         fg=_PROP_COLORS.get(label, WHITE),
                         font=("Segoe UI", 9, "bold")).pack(side="left")

            # DPS block (computed): right-aligned, blue, like the trade site
            for label, value in dps:
                row = tk.Frame(right, bg=CARD); row.pack(fill="x")
                tk.Label(row, text=f"{label}: ", bg=CARD, fg=MUTE,
                         font=("Segoe UI", 9)).pack(side="left")
                tk.Label(row, text=value, bg=CARD, fg=BLUE,
                         font=("Segoe UI", 10, "bold")).pack(side="right")

            self.summary_lbl.config(text="")
            self.web_btn.config(state="disabled")
            self.search_btn.config(state="disabled")
            for w in self.filters.winfo_children(): w.destroy()
            for w in self.prices.winfo_children(): w.destroy()
            self._fw = []
        self.root.after(0, u)

    def show_filters(self, filters, on_search):
        """One row per mod, grouped by section (Combined / Implicit / Modifiers)."""
        self._on_search = on_search
        def u():
            for w in self.filters.winfo_children(): w.destroy()
            self._fw = []
            if not filters:
                self.search_btn.config(state="normal")
                tk.Label(self.filters, text="Priced by name (no mod filters)",
                         bg=BG, fg=MUTE, font=("Segoe UI", 8)).pack(anchor="w")
                return
            # Quick presets (Awakened-style): one click re-targets every filter
            # and re-searches. Base = name/type only; ~90% = defaults at 90%;
            # Exact = everything checked at the item's exact rolls.
            pr = tk.Frame(self.filters, bg=BG); pr.pack(fill="x", pady=(2, 3))
            tk.Label(pr, text="Preset:", bg=BG, fg=MUTE,
                     font=("Segoe UI", 8)).pack(side="left", padx=(0, 6))
            for lbl, mode in (("Base", "base"), ("~90%", "p90"), ("Exact", "exact")):
                tk.Button(pr, text=lbl, command=lambda m=mode: self._apply_preset(m),
                          bg=CARD, fg=GOLD2, font=("Segoe UI", 8), relief="flat",
                          cursor="hand2", bd=0, padx=10, pady=1,
                          activebackground=GOLD, activeforeground="#1a1206"
                          ).pack(side="left", padx=(0, 5))
            last_sec = None
            for f in filters[:18]:
                sec = f.get("section", "Modifiers")
                if sec != last_sec:                       # group header
                    last_sec = sec
                    h = tk.Frame(self.filters, bg=BG); h.pack(fill="x", pady=(5, 1))
                    tk.Label(h, text=sec.upper(), bg=BG,
                             fg=PSEUDO_C if f.get("pseudo") else GOLD,
                             font=("Segoe UI", 7, "bold")).pack(side="left")
                    tk.Frame(h, bg=LINE, height=1).pack(side="left", fill="x",
                             expand=True, padx=(6, 0), pady=5)

                row = tk.Frame(self.filters, bg=PANEL); row.pack(fill="x", pady=1)
                var = tk.BooleanVar(value=f["enabled"])
                tk.Checkbutton(row, variable=var, bg=PANEL, activebackground=PANEL,
                               selectcolor=GOLD, highlightthickness=0, bd=0
                               ).pack(side="left", padx=(2, 2))
                txt = f["text"].replace("#", _fmt(f["value"]) if f["value"] is not None else "#")
                tk.Label(row, text=txt, bg=PANEL,
                         fg=PSEUDO_C if f.get("pseudo") else WHITE,
                         font=("Segoe UI", 9), anchor="w",
                         wraplength=290, justify="left").pack(side="left", fill="x",
                         expand=True, pady=2)
                slider = None
                dom = slider_domain(f["text"], f["value"])
                if dom:
                    lo, hi = dom
                    # Default min = filter's min_default (item roll for uniques),
                    # clamped to the slider domain.
                    md = f.get("min_default")
                    start = lo if md is None else max(lo, min(md, hi))
                    slider = RangeSlider(row, lo, hi, start, hi)
                    slider.pack(side="right", padx=(4, 4))
                self._fw.append((var, slider, f))
            self.search_btn.config(state="normal")
        self.root.after(0, u)

    def active_filters(self):
        out = []
        for var, slider, f in self._fw:
            if not var.get():
                continue
            if slider is not None:
                mn, mx = slider.get()
            else:
                mn, mx = f.get("value"), None
            key = "equip" if f.get("equip") else "id"
            out.append({key: f[key], "min": mn, "max": mx})
        return out

    def _fire_search(self):
        if self._on_search:
            threading.Thread(target=self._on_search, daemon=True).start()

    def _apply_preset(self, mode):
        """One-click filter presets: 'base' (name/type only), 'p90' (defaults
        at 90% of the item's values), 'exact' (everything at exact rolls)."""
        for var, slider, f in self._fw:
            val = f.get("value")
            if mode == "base":
                var.set(False)
                continue
            if mode == "p90":
                var.set(bool(f.get("enabled")))
                target = val * 0.9 if val is not None else None
            else:  # exact
                var.set(val is not None)
                target = val
            if slider is not None and target is not None:
                slider.vmin = max(slider.lo, min(float(target), slider.hi))
                slider.vmax = slider.hi
                slider._draw()
        self._fire_search()

    # --- recent-checks history (compact, clickable) ---
    def _add_history(self, name, rarity, summary, url):
        # replace an older entry for the same item instead of duplicating
        self._history = deque((h for h in self._history if h["name"] != name),
                              maxlen=self._history.maxlen)
        self._history.appendleft({"name": name, "rarity": rarity,
                                  "summary": summary, "url": url})
        self.root.after(0, self._render_history)

    def _render_history(self):
        for w in self.history_frame.winfo_children():
            w.destroy()
        for h in self._history:
            rw = tk.Frame(self.history_frame, bg=BG)
            rw.pack(fill="x")
            color = RARITY_COLOR.get(h["rarity"], WHITE)
            name = tk.Label(rw, text=h["name"][:34], bg=BG, fg=color,
                            font=("Segoe UI", 8), anchor="w",
                            cursor="hand2" if h["url"] else "")
            name.pack(side="left")
            tk.Label(rw, text=h["summary"], bg=BG, fg=MUTE,
                     font=("Segoe UI", 8), anchor="e").pack(side="right")
            if h["url"]:
                url = h["url"]
                name.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def wake_at(self, x, y):
        """
        Show the window on the OPPOSITE half of the screen from the cursor.
        The game tooltip pops up next to the hovered item, so going
        contralateral keeps it readable almost always; avoid_rect() then
        fine-tunes once the tooltip's real position is known.
        """
        def u():
            self.pill.withdraw()
            self.root.update_idletasks()
            w = self.root.winfo_width() or 520
            h = self.root.winfo_height() or 900
            sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
            nx = 24 if x > sw / 2 else sw - w - 24
            ny = max(8, min(y - 120, sh - h - 48))
            self.root.geometry(f"+{int(nx)}+{int(ny)}")
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
        self.root.after(0, u)

    def avoid_rect(self, rect, margin=18):
        """If the window overlaps `rect` (screen coords: x0,y0,x1,y1 — the
        game tooltip), slide it to whichever side has more free space."""
        def u():
            if not self.root.winfo_ismapped():
                return
            self.root.update_idletasks()
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            ww, wh = self.root.winfo_width(), self.root.winfo_height()
            rx0, ry0, rx1, ry1 = rect
            # no overlap -> nothing to do
            if (wx > rx1 + margin or wx + ww < rx0 - margin or
                    wy > ry1 + margin or wy + wh < ry0 - margin):
                return
            sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
            space_left, space_right = rx0, sw - rx1
            if space_right >= space_left:
                nx = min(rx1 + margin, sw - ww - 8)
            else:
                nx = max(8, rx0 - margin - ww)
            ny = max(8, min(wy, sh - wh - 48))
            self.root.geometry(f"+{int(nx)}+{int(ny)}")
        self.root.after(0, u)

    def set_hotkey_hint(self, check, hide):
        self.root.after(0, lambda: self.hint_lbl.config(
            text=f"{check.upper()} check · {hide.upper()} hide"))

    def _history_after_result(self):
        """Runs right after show_result renders: log the check in RECENT."""
        summ = self.summary_lbl.cget("text")
        if self._last_name and summ and summ not in ("0", "⏳"):
            self._add_history(self._last_name, self._last_rarity, summ, self._url)

    def show_result(self, result, league):
        prices = result.get("prices", [])
        total = result.get("total", 0)
        self._url = result.get("url")
        if prices and "error" not in result:
            self.root.after(30, self._history_after_result)
        def u():
            for w in self.prices.winfo_children(): w.destroy()
            self.web_btn.config(state="normal" if self._url else "disabled")
            if "error" in result:
                if result.get("rate_limited"):
                    secs = result.get("retry") or "?"
                    self.summary_lbl.config(text="⏳")
                    tk.Label(self.prices,
                             text=f"⏳ Rate limited by the trade API — wait {secs}s",
                             bg=BG, fg=CORRUPT, font=("Segoe UI", 10, "bold"),
                             wraplength=470, justify="left").pack(anchor="w", pady=(2, 1))
                    tk.Label(self.prices,
                             text="Too many searches. It auto-recovers; just wait and retry.",
                             bg=BG, fg=MUTE, font=("Segoe UI", 8),
                             wraplength=470, justify="left").pack(anchor="w")
                else:
                    self.summary_lbl.config(text="")
                    tk.Label(self.prices, text="API: " + result["error"], bg=BG, fg=CORRUPT,
                             font=("Segoe UI", 8), wraplength=470, justify="left").pack(anchor="w")
                return
            if not prices:
                self.summary_lbl.config(text="0")
                tk.Label(self.prices, text="No results with these filters — loosen some",
                         bg=BG, fg=MUTE, font=("Segoe UI", 9)).pack(anchor="w"); return
            # Fallback warning (e.g. base price shown because filters gave 0)
            note = result.get("note")
            if note:
                tk.Label(self.prices, text="⚠ " + note, bg=BG, fg="#f0a030",
                         font=("Segoe UI", 9, "bold"), wraplength=470,
                         justify="left").pack(anchor="w", pady=(0, 4))
            # poe2scout: real exchange price + history
            if result.get("scout"):
                price = prices[0]["amount"]
                self.summary_lbl.config(text=f"{_fmt(price)} ex")
                tk.Label(self.prices,
                         text=f"Currency Exchange price · vol {total}/day · {league} league",
                         bg=BG, fg=GREEN, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 3))
                # Price history (latest points)
                logs = result.get("price_logs", [])
                pts = [l for l in logs if l and l.get("Price")][:6]
                if len(pts) > 1:
                    tk.Label(self.prices, text="History (newest → oldest):",
                             bg=BG, fg=MUTE, font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 1))
                    for l in pts:
                        rw = tk.Frame(self.prices, bg=BG); rw.pack(fill="x")
                        t = l.get("Time", "")[:10]
                        tk.Label(rw, text=f"{_fmt(round(l['Price'],2))} ex",
                                 bg=BG, fg=WHITE, font=("Segoe UI", 9, "bold"),
                                 width=12, anchor="w").pack(side="left", padx=(6, 0))
                        tk.Label(rw, text=f"{t}  ·  vol {l.get('Quantity','?')}",
                                 bg=BG, fg=MUTE, font=("Segoe UI", 8),
                                 anchor="w").pack(side="left")
                return
            # Exchange (trade2 API): volume-weighted market ratio
            mr = result.get("market_ratio")
            if mr:
                cur = prices[0]["currency"] if prices else "?"
                self.summary_lbl.config(text=f"~{_fmt(mr)} {cur}")
                tk.Label(self.prices, text=f"Market ratio: {_fmt(mr)}:1 · {total} offers · {league} league",
                         bg=BG, fg=MUTE, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 3))
            else:
                lo, med, cur = _summary(prices)
                summ = f"{_fmt(lo)}–{_fmt(med)} {cur}"
                dh = _div_hint(med, cur, self.divine_price)
                if dh:
                    summ += f"  ({dh})"
                self.summary_lbl.config(text=summ)
                tk.Label(self.prices, text=f"{total} listed · {league} league", bg=BG, fg=MUTE,
                         font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 3))
            for i, p in enumerate(prices[:9]):
                rw = tk.Frame(self.prices, bg=CARD if i % 2 == 0 else BG)
                rw.pack(fill="x", pady=1)
                price_text = f"{_fmt(p['amount'])} {p['currency']}"
                if p.get("stock"):
                    price_text += f"  ×{p['stock']}"
                tk.Label(rw, text=price_text,
                         bg=rw["bg"], fg=GOLD2 if i == 0 else WHITE,
                         font=("Segoe UI", 11 if i == 0 else 10, "bold"),
                         width=16, anchor="w").pack(side="left", padx=(6, 0))
                # ex -> div hint for expensive listings
                dh = _div_hint(p["amount"], p["currency"], self.divine_price)
                if dh:
                    tk.Label(rw, text=dh, bg=rw["bg"], fg=GOLD2 if i == 0 else MUTE,
                             font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
                # badges: ⚡ instant-buyout listing, 🔥 in demand, price type
                if p.get("instant"):
                    tk.Label(rw, text="⚡", bg=rw["bg"], fg=GOLD2,
                             font=("Segoe UI", 8)).pack(side="left")
                if p.get("demand"):
                    tk.Label(rw, text="🔥", bg=rw["bg"], fg=CORRUPT,
                             font=("Segoe UI", 7)).pack(side="left")
                pt = p.get("ptype")
                if pt:
                    tk.Label(rw, text=pt, bg=rw["bg"],
                             fg=GREEN if pt == "b/o" else MUTE,
                             font=("Segoe UI", 7)).pack(side="left", padx=(0, 4))
                tk.Label(rw, text=p["account"], bg=rw["bg"], fg=MUTE,
                         font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True)
        self.root.after(0, u)

    def close(self): self.root.after(0, self.root.destroy)


def _fmt(x):
    try:
        x = float(x)
        return str(int(x)) if x.is_integer() else f"{x:g}"
    except Exception:
        return str(x)


def _summary(prices):
    from collections import Counter
    cur = Counter(p["currency"] for p in prices).most_common(1)[0][0]
    vals = sorted(float(p["amount"]) for p in prices if p["currency"] == cur)
    return vals[0], statistics.median(vals), cur


# Item properties parsed from the tooltip, listed like the trade website:
# one labeled line each, in tooltip order. Each: (label, regex).
_PROP_PATTERNS = [
    ("Quality",             r"qualit[yi]\s*:?\s*\+?(\d+)\s*%?"),
    ("Physical Damage",     r"physical damage\s*:?\s*([\d]+\s*[-–]\s*[\d]+)"),
    ("Fire Damage",         r"fire damage\s*:?\s*([\d]+\s*[-–]\s*[\d]+)"),
    ("Cold Damage",         r"cold damage\s*:?\s*([\d]+\s*[-–]\s*[\d]+)"),
    ("Lightning Damage",    r"lightning damage\s*:?\s*([\d]+\s*[-–]\s*[\d]+)"),
    ("Chaos Damage",        r"chaos damage\s*:?\s*([\d]+\s*[-–]\s*[\d]+)"),
    ("Critical Hit Chance", r"critical hit chance\s*:?\s*([\d.]+)\s*%?"),
    ("Attacks per Second",  r"attacks per second\s*:?\s*([\d.]+)"),
    ("Armour",              r"\barmou?r\s*:?\s*(\d+)"),
    ("Evasion Rating",      r"evasion rating\s*:?\s*(\d+)"),
    ("Energy Shield",       r"energy shield\s*:?\s*(\d+)"),
    ("Runic Ward",          r"runic ward\s*:?\s*(\d+)"),
    ("Spirit",              r"\bspirit\s*:?\s*(\d+)"),
    ("Item Level",          r"item level\s*:?\s*(\d+)"),
]

# Value colors per property, echoing the in-game/trade-site palette.
_PROP_COLORS = {
    "Fire Damage": "#e05545", "Cold Damage": "#7db8e8",
    "Lightning Damage": "#ead85e", "Chaos Damage": "#c577d6",
}


def _parse_item_props(lines):
    """
    Parse displayable item properties from the tooltip lines, one labeled line
    each (trade-website style). Returns an ordered list of (label, value).
    """
    props = []
    for label, pat in _PROP_PATTERNS:
        # Match within a single line so values never bleed across newlines.
        val = None
        for ln in lines:
            m = re.search(pat, ln, re.I)
            if m:
                val = m.group(1); break
        if val is None:
            continue
        if label == "Quality":
            val = f"+{val}%"
        elif label == "Critical Hit Chance":
            val = f"{val}%"
        props.append((label, re.sub(r"\s", "", val) if "-" in str(val) else val))
    # Requirements (Level / attributes) — keep original capitalization
    for ln in lines:
        m = re.search(r"requires?\s*:?\s*(.+)", ln, re.I)
        if m:
            props.append(("Requires", re.sub(r"\s+", " ", m.group(1)).strip()[:42]))
            break
    return props


def _rarity_from_lines(raw):
    """No color in OCR, so estimate rarity by mod count (rare>=3, magic 1-2)."""
    mods = len(re.findall(r"(increased|reduced|resistance|to maximum|added|gain|\+\d)", raw, re.I))
    if mods >= 3:
        return "rare"
    if mods >= 1:
        return "magic"
    return "normal"


# ── Pipeline ──────────────────────────────────────────────────────────────────

_state = {"league": None, "db": None, "stats": None,
          "db_index": None, "stat_index": None}


def price_check(ov: Overlay):
    import time
    t0 = time.time()
    ov.set_status("Capturing...", "#aaa")
    cx, cy = cursor_pos()          # where the user is pointing (for placement)
    ov.sleep(show_pill=False)      # hide everything so nothing of ours is in the shot
    time.sleep(0.08)
    screen, ox, oy = capture_screen()
    # We have the shot: show the window again, next to the cursor if configured
    if _state.get("cfg", {}).get("near_cursor", True):
        ov.wake_at(cx, cy)
    else:
        ov.wake()

    ov.set_status("Screen OCR...", "#aaa")
    try:
        lines_pos = run_ocr_fullscreen(screen)   # OCR the WHOLE screen, with positions
    except Exception as e:
        ov.set_status(f"OCR error: {e}", "#d00"); return
    if not lines_pos:
        ov.set_status("Empty OCR. Retry (F5).", "#f80"); return

    # Find the item by its NAME among all screen lines
    item, lines, guess = resolve_fullscreen(lines_pos, _state["db"], _state["db_index"])
    raw = "\n".join(lines)
    corrupted = "corrupt" in raw.lower()
    try:   # debug: dump the collected tooltip lines for diagnostics
        with open(os.path.join(TMP, "last_lines.txt"), "w", encoding="utf-8") as _f:
            _f.write("\n".join(lines))
    except Exception:
        pass
    props = _parse_item_props(lines)
    if not item:
        hint = f"OCR read: {guess}" if guess else "—"
        ov.show_item("Item not recognized", hint, corrupted, rarity="normal", props=props)
        ov.set_status("Not identified — re-point at the item and press F5 again.", "#f80")
        return

    # Now that we know where the game tooltip actually is, make sure the
    # overlay isn't covering it (slide aside only if they overlap).
    bbox = item.get("tooltip_bbox")
    if bbox:
        ov.avoid_rect((bbox[0] + ox, bbox[1] + oy, bbox[2] + ox, bbox[3] + oy))

    rarity = "unique" if item["unique"] else _rarity_from_lines(raw)
    category = item.get("category", "")
    display_name = item["name"] or item["type"]
    # Gems with a level: show the type with level (already in the DB type)
    if item.get("gem_level") and "level" not in item["type"].lower():
        display_name = f'{item["type"]} (Level {item["gem_level"]})'
    # Computed DPS block for the card (weapons), like the trade site's right side
    equip_all = compute_equipment_filters(lines)
    dps_block = [tuple(f["text"].split(": ", 1)) for f in equip_all
                 if f["equip"] in ("dps", "pdps", "edps")]
    ov.show_item(display_name, item["type"], corrupted,
                 rarity=rarity, props=props, dps=dps_block)

    league = _state["league"]

    # Currencies and gems: in-game Currency Exchange price via poe2scout
    # (real volume-weighted price). Falls back to the normal trade API.
    if category in ("currency", "gems"):
        def do_search(initial=False):
            ov.set_status("Exchange price…", "#aaa")
            res = search_poe2scout(item, league)
            if "error" in res and not res.get("rate_limited"):
                # Fall back to the normal trade API (poe2scout had no data)
                ov.set_status("Searching trade…", "#aaa")
                res = search_trade(item, league, [], exact=True,
                                   options=ov.active_options())
            ov.show_result(res, league)
            if res.get("rate_limited"):
                ov.set_status(f"⏳ Rate limited — wait {res.get('retry','?')}s", "#f80")
            elif "error" in res:
                ov.set_status(f"Error: {res['error'][:50]}", "#f80")
            elif res.get("prices"):
                if res.get("scout"):
                    ov.set_status(f"Real exchange price · vol {res['total']}/day", GREEN)
                else:
                    ov.set_status(f"{res['total']} on trade", GREEN)
            else:
                ov.set_status("0 results", "#f80")
        ov.show_filters([], do_search)
        do_search(initial=True)
        ov.set_status(f"Done in {time.time()-t0:.1f}s", GREEN)
        return

    # Match mods for both rares AND uniques (a unique's variable affixes are
    # also filtered by roll: a higher roll is worth more). For non-uniques we
    # also add the base-property filters (weapon DPS / armour defenses).
    matched = match_stats(lines, _state["stats"], _state["stat_index"])
    equip = [] if item["unique"] else equip_all
    filters = build_filters(item, matched, _state["stats"], equip=equip)

    # re-search callback: respects the checked filters + sale options.
    # Kept to AT MOST 2 API calls (exact, then unfiltered) to stay well within
    # the trade API rate limits. For roll-based pricing, loosen the sliders and
    # press Search manually.
    def _rate_limited(res):
        if res.get("rate_limited"):
            ov.show_result(res, league)
            ov.set_status(f"⏳ Rate limited — wait {res.get('retry','?')}s", "#f80")
            return True
        return False

    def do_search(initial=False):
        if initial:
            # Initial search: enabled filters, using each filter's min_default.
            # Each entry carries an 'equip' id (DPS/defenses) or a stat 'id'.
            active = []
            for f in filters:
                if not f["enabled"]:
                    continue
                key = "equip" if f.get("equip") else "id"
                active.append({key: f[key], "min": f.get("min_default"), "max": None})
        else:
            # Manual search: read what the user set in the UI
            active = ov.active_filters()
        opts = ov.active_options()
        ov.set_status("Searching…", "#aaa")
        res = search_trade(item, league, active, exact=True, options=opts)
        if _rate_limited(res):
            return
        if "error" in res:
            ov.show_result(res, league); ov.set_status("API error", "#f80"); return
        if res.get("prices"):
            ov.show_result(res, league)
            ov.set_status(f"{res['total']} results · {len(active)} filters", GREEN)
            return

        # 0 results with filters: one unfiltered fallback for a base price.
        if active:
            res2 = search_trade(item, league, [], exact=True, options=opts)
            if _rate_limited(res2):
                return
            if res2.get("prices"):
                res2["note"] = (f"0 exact matches with your {len(active)} filters — "
                                "showing the BASE price instead. "
                                "↗ Web opens YOUR filtered search; loosen it there.")
                ov.show_result(res2, league)
                # Keep the FILTERED query's URL on the ↗ Web button: the user
                # opens exactly what they configured (even at 0 results) and
                # can loosen it on the website.
                if res.get("url"):
                    ov._url = res["url"]
                ov.set_status(f"0 exact comps — base price ({res2['total']} listed). "
                              "Loosen sliders for roll pricing.", "#f80")
                return
        ov.show_result(res, league)
        ov.set_status("0 results — loosen filters", "#f80")

    ov.show_filters(filters, do_search)
    do_search(initial=True)
    ov.set_status(f"Done in {time.time()-t0:.1f}s — adjust and press Search", GREEN)


def main():
    import keyboard
    print(f"{APP_NAME} v{VERSION} — starting...")
    cfg = load_config()
    _state["cfg"] = cfg
    ov = Overlay()
    ov._cfg = cfg

    # System tray icon (next to the clock): standard "I'm running" indicator.
    # Optional dependency — the app still works if pystray isn't installed.
    def _tray():
        try:
            import pystray
            from PIL import ImageDraw
            img = Image.new("RGB", (64, 64), "#0f0e0c")
            d = ImageDraw.Draw(img)
            d.polygon([(32, 6), (58, 32), (32, 58), (6, 32)], fill="#c79a4b")
            d.polygon([(32, 20), (44, 32), (32, 44), (20, 32)], fill="#0f0e0c")
            menu = pystray.Menu(
                pystray.MenuItem("Show overlay", lambda: ov.wake(), default=True),
                pystray.MenuItem("Settings", lambda: ov.open_settings()),
                pystray.MenuItem("Quit", lambda: os._exit(0)))
            icon = pystray.Icon(APP_ID, img, f"{APP_NAME} v{VERSION}", menu)
            _state["tray"] = icon
            icon.run()
        except Exception as e:
            print("Tray unavailable:", e)
    threading.Thread(target=_tray, daemon=True).start()

    ov.set_status("Loading league and databases...", "#aaa")
    # League from config, or auto-detect the current one
    _state["league"] = cfg.get("league") or get_league()
    ov.divine_price = get_divine_price(_state["league"])
    # League list for the Settings selector (background; non-critical)
    threading.Thread(target=lambda: setattr(ov, "leagues", get_leagues()),
                     daemon=True).start()

    def reload_dbs(force=False):
        _state["db"] = load_item_db(force=force)
        _state["stats"] = load_stat_db(force=force)
        _state["db_index"] = build_index(_state["db"])
        _state["stat_index"] = build_index(_state["stats"])

    try:
        reload_dbs()
    except Exception as e:
        _state["db"] = _state["db"] or []; _state["stats"] = _state["stats"] or []
        _state["db_index"] = _state["db_index"] or {}; _state["stat_index"] = _state["stat_index"] or {}
        print("Error loading DB:", e)
    div = f" | div≈{_fmt(round(ov.divine_price))}ex" if ov.divine_price else ""
    print(f"League: {_state['league']}  |  {len(_state['db'])} items  |  {len(_state['stats'])} stats{div}")
    _hk_check = cfg.get("hotkeys", _DEFAULT_CONFIG["hotkeys"])["check"].upper()
    ov.set_status(f"{_state['league']}  |  {len(_state['db'])} items  |  {_hk_check} = check", BLUE)
    ov.pill_set(_state["league"])
    tray = _state.get("tray")
    if tray:
        tray.title = f"{APP_NAME} v{VERSION} — {_state['league']}"
    # Show the welcome card once at startup so users SEE it's running;
    # clicking into the game auto-hides it and leaves the status pill.
    ov.wake()

    def refresh_db():
        ov.set_status("Refreshing DBs...", "#aaa")
        try:
            reload_dbs(force=True)
            ov.divine_price = get_divine_price(_state["league"])
            ov.set_status(f"DBs OK: {len(_state['db'])} items, {len(_state['stats'])} stats", GREEN)
        except Exception as e:
            ov.set_status(f"DB error: {e}", "#d00")

    # Hotkeys from config; re-registrable live from the Settings window.
    def register_hotkeys(hk):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        keyboard.add_hotkey(hk["check"], lambda: threading.Thread(
            target=price_check, args=(ov,), daemon=True).start())
        keyboard.add_hotkey(hk["web"], ov.open_web)
        keyboard.add_hotkey(hk["refresh"], lambda: threading.Thread(
            target=refresh_db, daemon=True).start())
        keyboard.add_hotkey(hk["hide"], ov.sleep)
        keyboard.add_hotkey(hk["quit"], lambda: os._exit(0))
        ov.set_hotkey_hint(hk["check"], hk["hide"])

    def apply_settings(new_cfg):
        """Called by the Settings window: applies league/hotkeys live."""
        _state["cfg"] = new_cfg
        ov._cfg = new_cfg
        _state["league"] = new_cfg.get("league") or get_league()
        ov.divine_price = get_divine_price(_state["league"])
        ov.pill_set(_state["league"])
        if not new_cfg.get("pill", True):
            ov.root.after(0, ov.pill.withdraw)
        try:
            register_hotkeys(new_cfg.get("hotkeys", _DEFAULT_CONFIG["hotkeys"]))
        except Exception as e:
            ov.set_status(f"Hotkey error: {e}", "#d00")
            return
        ov.set_status(f"Settings applied — {_state['league']}", GREEN)

    ov.on_settings_saved = apply_settings
    hk = cfg.get("hotkeys", _DEFAULT_CONFIG["hotkeys"])
    print(f"Ready. Sleeping in the background — press {hk['check'].upper()} over an item.")
    register_hotkeys(hk)
    keyboard.wait()


if __name__ == "__main__":
    main()
