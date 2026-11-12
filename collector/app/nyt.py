"""Headless browser that reads today's puzzle and your progress.

Each sync launches Chromium with your NYT session cookie (see session.py),
loads the Spelling Bee page, reads `window.gameData` for the puzzle, and works
out which words you've found from the game-state JSON the page fetches
(falling back to the on-page word list).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Response, async_playwright

from .nyt_types import Snapshot
from .session import SESSION_COOKIE, current_cookies

log = logging.getLogger("collector.nyt")

GAME_URL = "https://www.nytimes.com/puzzles/spelling-bee"
BUDDY_URL = "https://www.nytimes.com/interactive/2023/upshot/spelling-bee-buddy.html"

PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", "/profile"))
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "/debug"))
DEBUG_CAPTURE = os.environ.get("DEBUG_CAPTURE", "0") == "1"
# Buddy's visitor stats: re-read when your found words change, or after this long.
VISITOR_STATS = os.environ.get("VISITOR_STATS", "1") == "1"
VISITOR_STATS_MAX_AGE = int(os.environ.get("VISITOR_STATS_MINUTES", "15")) * 60

_visitors_cache: dict = {"key": None, "at": 0.0, "data": None}

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


async def _visitor_stats(page: Page, found: set[str], puzzle_id) -> dict | None:
    """Buddy's visitor table, re-read only when it could have changed."""
    key = (puzzle_id, frozenset(found))
    fresh = time.time() - _visitors_cache["at"] < VISITOR_STATS_MAX_AGE
    if _visitors_cache["key"] == key and fresh:
        return _visitors_cache["data"]
    try:
        await page.goto(BUDDY_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector(".reader-stats table tbody tr .bar-fg", timeout=45_000)
        data = await page.evaluate(_VISITORS_JS)
    except Exception as exc:
        log.warning("Couldn't read Buddy's visitor stats: %s", exc)
        return _visitors_cache["data"] if _visitors_cache["key"] and _visitors_cache["key"][0] == puzzle_id else None
    if data and data.get("rows"):
        _visitors_cache.update(key=key, at=time.time(), data=data)
        log.info("Read visitor stats for %d words", len(data["rows"]))
    return data


async def snapshot() -> Snapshot:
    captured: list[dict] = []   # JSON responses; your found words are in one of them
    debug_log: list[dict] = []  # every fetch/XHR in debug mode, whatever its type

    async def on_response(resp: Response):
        if "nytimes.com" not in resp.url:
            return
        ctype = resp.headers.get("content-type", "")
        if DEBUG_CAPTURE and resp.request.resource_type in ("fetch", "xhr"):
            entry = {"url": resp.url, "status": resp.status, "type": ctype}
            try:
                entry["text"] = (await resp.text())[:2_000_000]
            except Exception as exc:
                entry["error"] = str(exc)
            debug_log.append(entry)
        if "json" not in ctype:
            return
        try:
            captured.append({"url": resp.url, "body": await resp.json()})
        except Exception:
            pass

    async with async_playwright() as pw:
        ctx = await _launch(pw)
        try:
            page = await ctx.new_page()
            logged_in = await _load_session(ctx)

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

            visitors = None
            if logged_in and VISITOR_STATS:
                visitors = await _visitor_stats(page, found, game.get("id"))

            if DEBUG_CAPTURE:
                # Make sure Buddy itself was loaded, to see what data it fetches.
                if logged_in and visitors is None:
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
                # Buddy may also keep its data inside the page itself.
                try:
                    (DEBUG_DIR / "buddy.html").write_text(await page.content())
                except Exception:
                    pass
                await _debug_shot(page, "last-sync")

            return Snapshot(game=game, found=found, logged_in=logged_in,
                            found_source=source, visitors=visitors)
        finally:
            await ctx.close()
