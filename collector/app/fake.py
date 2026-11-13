"""Stand-in for nyt.NYT so the stack can be tried without NYT (FAKE_NYT=1).

Pretends you find one more word every sync, once any cookie has been saved.
"""
import asyncio

from .buddy import visitors_from_stats
from .nyt_types import Snapshot
from .session import current_cookies

GAME = {
    "id": 99999,
    "printDate": "2026-01-01",
    "centerLetter": "a",
    "outerLetters": ["c", "l", "n", "o", "t", "i"],
    "pangrams": ["location"],
    "answers": [
        "location", "action", "cation", "talon", "tonal", "canal", "local", "natal",
        "cant", "coal", "coat", "cola", "loan", "talc", "tall", "taco", "alto",
        "nail", "tail", "lain", "anion", "latino", "cantina", "italian", "tactical",
    ],
}
_found: list[str] = []


class FakeNYT:
    """Same interface as nyt.NYT, with a built-in puzzle."""

    async def close(self) -> None:
        pass

    async def snapshot(self) -> Snapshot:
        await asyncio.sleep(0.3)
        logged_in = bool(current_cookies()[0])
        remaining = [a for a in GAME["answers"] if a not in _found]
        if logged_in and remaining:
            _found.append(remaining[0])
        found = set(_found) if logged_in else set()
        return Snapshot(game=GAME, found=found, logged_in=logged_in, found_source="fake")

    async def quick_visitor_stats(self, puzzle_id) -> dict | None:
        await asyncio.sleep(0.05)
        counts = {w: 9700 - i * 310 for i, w in enumerate(GAME["answers"])}
        return visitors_from_stats({"id": puzzle_id, "answers": counts, "n": 10000})

    async def visitor_stats(self) -> dict | None:
        await asyncio.sleep(2)
        rows = []
        for i, w in enumerate(GAME["answers"]):
            pct = round(97 - i * 3.1, 2)
            rows.append({"found": True, "text": w, "pct": pct} if w in _found
                        else {"found": False, "text": f"{w[0].upper()} ({len(w)})", "pct": pct})
        return {"rows": rows, "sample": 10000}
