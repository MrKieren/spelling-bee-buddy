"""Stand-in for nyt.snapshot() so the stack can be tried without NYT (FAKE_NYT=1).

Pretends you find one more word every sync, once any cookie has been saved.
"""
import asyncio

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


async def snapshot() -> Snapshot:
    await asyncio.sleep(2)
    logged_in = bool(current_cookies()[0])
    remaining = [a for a in GAME["answers"] if a not in _found]
    if logged_in and remaining:
        _found.append(remaining[0])
    found = set(_found) if logged_in else set()
    rows = []
    for i, w in enumerate(GAME["answers"]):
        pct = round(97 - i * 3.1, 2)
        rows.append({"found": True, "text": w, "pct": pct} if w in found
                    else {"found": False, "text": f"{w[0].upper()} ({len(w)})", "pct": pct})
    visitors = {"rows": rows, "sample": 10000} if logged_in else None
    return Snapshot(game=GAME, found=found, logged_in=logged_in,
                    found_source="fake", visitors=visitors)
