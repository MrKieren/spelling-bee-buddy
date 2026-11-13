"""Headless browser that reads today's puzzle and your progress.

One Chromium instance stays running with your NYT session cookie (see
session.py), with a tab parked on nytimes.com. A normal sync doesn't
load any page: from that tab it fetches the same two small JSON files the game
itself uses (today's puzzle and your game state), which takes well under a
second.

If that fails, it falls back to the original method: load the game page in a
fresh tab, read `window.gameData`, and work out your words from the JSON the
page fetches (or the on-page word list).

Buddy's visitor stats need Buddy's page to render, which is slow, so main.py
reads them separately in the background (`visitor_stats`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Response, async_playwright

from .buddy import visitors_from_stats
from .nyt_types import Snapshot
from .session import SESSION_COOKIE, current_cookies

log = logging.getLogger("collector.nyt")

GAME_URL = "https://www.nytimes.com/puzzles/spelling-bee"
PUZZLE_API = "https://www.nytimes.com/svc/spelling-bee/v1/active.json"
# A plain-text NYT page to park the tab on: same origin as the APIs, but no
# scripts or ads running in the background.
PARK_URL = "https://www.nytimes.com/robots.txt"
STATE_API = "https://www.nytimes.com/svc/games/state/spelling_bee/latest?puzzle_id={id}"
# Bee Buddy's own per-puzzle visitor stats: {"answers": {word: count}, "n": 10000, ...}
STATS_API = "https://static01.nyt.com/newsgraphics/2023-01-18-spelling-bee-buddy/stats/{id}.json"
STATS_CACHE_SECONDS = 300
# Today's puzzle only changes once a day (3 a.m. Eastern), so it's kept until
# the puzzle date rolls over, and re-checked at least this often anyway.
PUZZLE_CACHE_SECONDS = 3600
_NYT_TZ = ZoneInfo("America/New_York")


def current_puzzle_date() -> str:
    """The date of the puzzle NYT is serving now: a new one goes live at 3 a.m. Eastern."""
    return (datetime.now(_NYT_TZ) - timedelta(hours=3)).date().isoformat()
BUDDY_URL = "https://www.nytimes.com/interactive/2023/upshot/spelling-bee-buddy.html"

PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", "/profile"))
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "/debug"))
DEBUG_CAPTURE = os.environ.get("DEBUG_CAPTURE", "0") == "1"
# When loading Buddy for its visitor stats, only NYT's own page, scripts, styles
# and data are needed: skip ads, trackers, images and fonts to load it faster.
_STATS_HOSTS = ("nytimes.com", "nyt.com")
_STATS_SKIP_TYPES = {"image", "media", "font"}


async def _lean_route(route) -> None:
    req = route.request
    host = req.url.split("/")[2].split(":")[0] if "://" in req.url else ""
    ours = any(host == h or host.endswith("." + h) for h in _STATS_HOSTS)
    if not ours or req.resource_type in _STATS_SKIP_TYPES:
        await route.abort()
    else:
        await route.fallback()


# Restart the browser this often, so a long-running Chromium can't build up memory.
BROWSER_MAX_AGE = int(os.environ.get("BROWSER_RESTART_HOURS", "6")) * 3600

# Fetches JSON from inside the parked NYT tab, so it's same-origin and carries
# the browser's cookies exactly like the game's own requests.
_FETCH_JS = """
async ([url, credentials]) => {
  const r = await fetch(url, { credentials, headers: { accept: 'application/json' } });
  let body = null;
  try { body = await r.json(); } catch (e) {}
  return { status: r.status, body };
}
"""

# Reads Buddy's rendered "You vs. Other Bee Buddy Visitors" table.
_VISITORS_JS = """
() => {
  const box = document.querySelector('.reader-stats');
  if (!box) return null;
  const rows = [];
  for (const tr of box.querySelectorAll('table tbody tr')) {
    const word = tr.querySelector('td.word');
    const bar = tr.querySelector('.bar-fg');
    if (!word || !bar) continue;
    rows.push({
      found: tr.classList.contains('user-found'),
      text: word.textContent.trim(),
      pct: parseFloat(bar.style.width),
    });
  }
  const m = box.textContent.match(/sample of ([\\d,]+) responses/);
  return { rows, sample: m ? parseInt(m[1].replace(/,/g, ''), 10) : null };
}
"""

_user_agent: str | None = None


async def _launch(pw) -> BrowserContext:
    """Persistent context with 'HeadlessChrome' scrubbed from the user agent."""
    global _user_agent
    args = ["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
    if _user_agent is None:
        probe = await pw.chromium.launch(headless=True, args=args)
        page = await probe.new_page()
        _user_agent = (await page.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
        await probe.close()
    return await pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=True,
        args=args,
        user_agent=_user_agent,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )


async def _logged_in(ctx: BrowserContext) -> bool:
    return any(c["name"] == SESSION_COOKIE for c in await ctx.cookies("https://www.nytimes.com"))


_last_source: tuple | None = None


async def _load_session(ctx: BrowserContext) -> bool:
    """Apply the newest saved NYT cookie (it always wins over whatever the
    browser profile has), then report whether a session cookie is present."""
    global _last_source
    cookies, source = current_cookies()
    if cookies:
        await ctx.add_cookies(cookies)
        if (source, cookies[0]["value"][-8:]) != _last_source:
            log.info("Using NYT cookie from %s", source)
            _last_source = (source, cookies[0]["value"][-8:])
    else:
        log.warning("No NYT cookie yet: paste one on the admin page")
    return await _logged_in(ctx)


async def _debug_shot(page: Page, name: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass


def _string_lists(obj, out: list[list[str]]) -> None:
    """Collect every list of strings anywhere inside a JSON value."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, str) for x in obj):
            out.append(obj)
        else:
            for x in obj:
                _string_lists(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _string_lists(v, out)


def _game_states(obj, out: list[dict]) -> None:
    """Collect every NYT game-state record ({"puzzle_id", "game_data": {...}})."""
    if isinstance(obj, dict):
        if isinstance(obj.get("game_data"), dict) and "puzzle_id" in obj:
            out.append(obj)
        for v in obj.values():
            _game_states(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _game_states(x, out)


def found_from_json(bodies: list, answers: set[str], pangrams: set[str], puzzle_id=None) -> set[str]:
    """Your found words for today's puzzle, from NYT's game-state JSON.

    First choice: the `game_data.answers` of the state whose `puzzle_id` is
    today's. That list is your found words, so it's trusted even when it holds
    every answer (Queen Bee).

    Fallback, if no state matches: the largest list of strings made only of
    answers, skipping lists equal to the full answer or pangram list, since
    those could be puzzle data rather than progress.
    """
    if puzzle_id is not None:
        states: list[dict] = []
        for body in bodies:
            _game_states(body, states)
        for st in states:
            if str(st.get("puzzle_id")) != str(puzzle_id):
                continue
            words = st["game_data"].get("answers")
            if isinstance(words, list):
                return {w.lower() for w in words if isinstance(w, str)} & answers

    best: set[str] = set()
    for body in bodies:
        lists: list[list[str]] = []
        _string_lists(body, lists)
        for lst in lists:
            words = {w.lower() for w in lst}
            if words in (answers, pangrams):
                continue
            if words and words <= answers and len(words) > len(best):
                best = words
    return best


async def _found_from_dom(page: Page, answers: set[str]) -> set[str]:
    """Word list in the game page (same selectors as Spelling Bee Helper)."""
    try:
        words = await page.eval_on_selector_all(
            ".sb-wordlist-drawer .sb-anagram, .sb-wordlist-items-pag .sb-anagram",
            "els => els.map(e => e.textContent.trim())",
        )
    except Exception:
        return set()
    # Strip a " (7)" length suffix if one has been added to the word.
    cleaned = {re.sub(r"\s*\(\d+\)$", "", w).lower() for w in words}
    return cleaned & answers


async def _start_game(page: Page) -> None:
    """Click through the welcome/resume modal so the word list renders."""
    for label in ("Continue", "Play", "Resume"):
        btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
        try:
            if await btn.first.is_visible(timeout=1_500):
                await btn.first.click()
                return
        except Exception:
            continue


def puzzle_from_active(active: dict) -> dict:
    """Today's puzzle from active.json, in the same shape as window.gameData.today."""
    today = active["today"]
    p = active["puzzles"][today] if isinstance(today, int) else today
    pangrams = [w.lower() for w in p.get("pangrams", [])]
    answers = sorted({w.lower() for w in p["answers"]} | set(pangrams))  # active.json lists pangrams separately
    outer = p["outer_letters"]
    return {
        "id": p["id"],
        "printDate": p.get("print_date"),
        "centerLetter": p["center_letter"],
        "outerLetters": list(outer) if isinstance(outer, str) else outer,
        "answers": answers,
        "pangrams": pangrams,
    }


class FastPathError(Exception):
    pass


class NYT:
    """Long-lived browser session. All methods are safe to call concurrently."""

    def __init__(self):
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._anchor: Page | None = None
        self._started = 0.0
        self._lock = asyncio.Lock()   # guards starting / restarting the browser
        self._lean_stats = True       # turned off if Buddy won't render without blocked resources
        self._stats_cache: dict = {}  # puzzle id -> (fetched at, visitors)
        self._puzzle: tuple | None = None  # (fetched at, game)

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        self._anchor = None
        if self._ctx:
            try:
                await self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._ctx = self._pw = None

    async def _context(self) -> BrowserContext:
        async with self._lock:
            alive = self._ctx is not None
            if alive and time.time() - self._started < BROWSER_MAX_AGE:
                try:
                    await self._ctx.cookies()   # cheap liveness check
                    return self._ctx
                except Exception:
                    log.warning("Browser went away; restarting it")
            await self._close()
            self._pw = await async_playwright().start()
            self._ctx = await _launch(self._pw)
            self._started = time.time()
            log.info("Browser started")
            return self._ctx

    async def _anchor_page(self, ctx: BrowserContext) -> Page:
        """The parked tab used for fast fetches; set up once, then reused.

        It opens the game page first (so NYT sets whatever cookies it sets for
        a normal visit), then parks on a plain-text NYT page so no scripts or
        ads keep running.
        """
        if self._anchor is None or self._anchor.is_closed():
            page = await ctx.new_page()
            await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.goto(PARK_URL, wait_until="domcontentloaded", timeout=30_000)
            self._anchor = page
        return self._anchor

    async def _drop_anchor(self) -> None:
        page, self._anchor = self._anchor, None
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass

    # ---------- syncing ----------

    async def snapshot(self) -> Snapshot:
        ctx = await self._context()
        logged_in = await _load_session(ctx)
        if not DEBUG_CAPTURE:
            try:
                return await self._fast(ctx, logged_in)
            except Exception as exc:
                log.warning("Quick sync failed (%s); loading the full page instead", exc)
                await self._drop_anchor()
        return await self._full(ctx, logged_in)

    async def _puzzle_game(self, page: Page) -> dict:
        """Today's puzzle, fetched once and reused until the puzzle date changes."""
        cached = self._puzzle
        if cached and cached[1].get("printDate") == current_puzzle_date() \
                and time.time() - cached[0] < PUZZLE_CACHE_SECONDS:
            return cached[1]
        r = await page.evaluate(_FETCH_JS, [PUZZLE_API, "include"])
        if r["status"] != 200 or not isinstance(r["body"], dict):
            raise FastPathError(f"puzzle API returned {r['status']}")
        game = puzzle_from_active(r["body"])
        self._puzzle = (time.time(), game)
        return game

    async def _fast(self, ctx: BrowserContext, logged_in: bool) -> Snapshot:
        page = await self._anchor_page(ctx)
        game = await self._puzzle_game(page)

        found, source = set(), "none"
        if logged_in:
            # Your words and (when due) the visitor stats, fetched side by side.
            stats_due = not self._stats_fresh(game["id"])
            state_req = page.evaluate(_FETCH_JS, [STATE_API.format(id=game["id"]), "include"])
            if stats_due:
                r, _ = await asyncio.gather(state_req, self._fetch_stats(page, game["id"]))
            else:
                r = await state_req
            if r["status"] == 200 and isinstance(r["body"], dict):
                answers = set(game["answers"])
                found = found_from_json([r["body"]], answers, set(game["pangrams"]), game["id"])
                source = "api-fast"
            elif r["status"] == 404:
                source = "api-fast"   # no state yet: you haven't played today's puzzle
            else:
                self._puzzle = None   # re-check the puzzle too on the next try
                raise FastPathError(f"game-state API returned {r['status']}")
        return Snapshot(game=game, found=found, logged_in=logged_in, found_source=source)

    async def _full(self, ctx: BrowserContext, logged_in: bool) -> Snapshot:
        captured: list[dict] = []   # JSON responses; your found words are in one of them
        debug_log: list[dict] = []  # every fetch/XHR in debug mode, whatever its type

        async def on_response(resp: Response):
            ctype = resp.headers.get("content-type", "")
            # Debug: record data requests to every domain (Buddy's own data may
            # come from nyt.com or a CDN, not just nytimes.com).
            if DEBUG_CAPTURE and (resp.request.resource_type in ("fetch", "xhr", "other")
                                  or any(t in ctype for t in ("json", "csv", "text/plain"))):
                entry = {"url": resp.url, "status": resp.status, "type": ctype}
                try:
                    entry["text"] = (await resp.text())[:2_000_000]
                except Exception as exc:
                    entry["error"] = str(exc)
                debug_log.append(entry)
            if "nytimes.com" not in resp.url or "json" not in ctype:
                return
            try:
                captured.append({"url": resp.url, "body": await resp.json()})
            except Exception:
                pass

        page = await ctx.new_page()
        try:
            page.on("response", on_response)
            await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_function("window.gameData && window.gameData.today", timeout=30_000)
            game = await page.evaluate("window.gameData.today")

            answers = {a.lower() for a in game["answers"]}
            found, source = set(), "none"
            if logged_in:
                await _start_game(page)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                state_bodies = [c["body"] for c in captured if "/svc/games" in c["url"]]
                pangrams = {p.lower() for p in game.get("pangrams", [])}
                found = found_from_json(state_bodies, answers, pangrams, game.get("id"))
                source = "api" if found else "none"
                dom = await _found_from_dom(page, answers)
                if dom - found:
                    found |= dom
                    source = "dom" if source == "none" else "api+dom"
            else:
                await _debug_shot(page, "not-logged-in")

            if DEBUG_CAPTURE:
                if logged_in:
                    try:
                        await page.goto(BUDDY_URL, wait_until="domcontentloaded", timeout=60_000)
                        await page.wait_for_load_state("networkidle", timeout=30_000)
                        await _debug_shot(page, "buddy")
                    except Exception as exc:
                        log.warning("Couldn't load Buddy for capture: %s", exc)
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                (DEBUG_DIR / f"capture-{int(time.time())}.json").write_text(
                    json.dumps(debug_log, indent=1, default=str)
                )
                try:
                    (DEBUG_DIR / "buddy.html").write_text(await page.content())
                except Exception:
                    pass
                await _debug_shot(page, "last-sync")

            return Snapshot(game=game, found=found, logged_in=logged_in, found_source=source)
        finally:
            await page.close()

    # ---------- visitor stats ----------

    def _stats_fresh(self, puzzle_id) -> bool:
        cached = self._stats_cache.get(puzzle_id)
        return bool(cached) and time.time() - cached[0] < STATS_CACHE_SECONDS

    async def _fetch_stats(self, page: Page, puzzle_id) -> dict | None:
        try:
            r = await page.evaluate(_FETCH_JS, [STATS_API.format(id=puzzle_id), "omit"])
        except Exception as exc:
            log.warning("Visitor stats file failed: %s", exc)
            return None
        data = visitors_from_stats(r["body"]) if r["status"] == 200 and isinstance(r["body"], dict) else None
        if data is None:
            log.warning("Visitor stats file unavailable (HTTP %s); using Buddy's page instead", r["status"])
            return None
        self._stats_cache = {puzzle_id: (time.time(), data)}
        return data

    async def quick_visitor_stats(self, puzzle_id) -> dict | None:
        """Bee Buddy's stats file for the puzzle: every word's share of
        visitors. Usually already fetched alongside the sync; cached for a
        few minutes."""
        if self._stats_fresh(puzzle_id):
            return self._stats_cache[puzzle_id][1]
        ctx = await self._context()
        page = await self._anchor_page(ctx)
        data = await self._fetch_stats(page, puzzle_id)
        if data is None:
            cached = self._stats_cache.get(puzzle_id)
            return cached[1] if cached else None
        return data

    # Slow fallback: read the stats from Buddy's rendered page.

    async def visitor_stats(self) -> dict | None:
        """Buddy's rendered "You vs. other visitors" table."""
        ctx = await self._context()
        await _load_session(ctx)
        if self._lean_stats:
            data = await self._read_visitors(ctx, lean=True, timeout=20_000)
            if data is not None:
                return data
        data = await self._read_visitors(ctx, lean=False, timeout=45_000)
        if data is not None and self._lean_stats:
            # Full load worked where the lean one didn't: Buddy needs something we block.
            log.warning("Buddy only renders with ads and images allowed; loading it in full from now on")
            self._lean_stats = False
        return data

    async def _read_visitors(self, ctx: BrowserContext, lean: bool, timeout: int) -> dict | None:
        page = await ctx.new_page()
        started = time.monotonic()
        try:
            if lean:
                await page.route("**/*", _lean_route)
            await page.goto(BUDDY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_selector(".reader-stats table tbody tr .bar-fg", state="attached", timeout=timeout)
            data = await page.evaluate(_VISITORS_JS)
        except Exception as exc:
            log.warning("Couldn't read Buddy's visitor stats: %s", exc)
            return None
        finally:
            await page.close()
        if data and data.get("rows"):
            log.info("Read visitor stats for %d words in %.1fs", len(data["rows"]), time.monotonic() - started)
            return data
        return None
