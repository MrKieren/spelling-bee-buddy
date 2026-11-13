"""Pure Spelling Bee Buddy maths. No I/O, no answers in the output.

build_public_data() turns the private puzzle (answers etc.) plus the set of
words you've found into the counts Buddy shows. The returned dict is what gets
published, so it must never contain unfound answers.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

# NYT rank thresholds as a fraction of the maximum score.
RANKS = [
    ("Beginner", 0.00),
    ("Good Start", 0.02),
    ("Moving Up", 0.05),
    ("Good", 0.08),
    ("Solid", 0.15),
    ("Nice", 0.25),
    ("Great", 0.40),
    ("Amazing", 0.50),
    ("Genius", 0.70),
    ("Queen Bee", 1.00),
]


def word_score(word: str, pangrams: set[str]) -> int:
    score = 1 if len(word) == 4 else len(word)
    if word in pangrams:
        score += 7
    return score


def rank_for(points: int, max_points: int) -> dict:
    thresholds = [(name, round(frac * max_points)) for name, frac in RANKS]
    current = thresholds[0]
    nxt = None
    for i, (name, need) in enumerate(thresholds):
        if points >= need:
            current = (name, need)
            nxt = thresholds[i + 1] if i + 1 < len(thresholds) else None
    return {
        "name": current[0],
        "next": nxt[0] if nxt else None,
        "points_to_next": (nxt[1] - points) if nxt else 0,
        "genius_at": dict(thresholds)["Genius"],
    }


_UNFOUND = re.compile(r"^([a-z])\s*\((\d+)\)$", re.I)


def public_visitors(raw: dict | None, found: set[str], answers: set[str]) -> dict | None:
    """Turn Buddy's visitor rows into public rows.

    A word is only named if it's in your found set; anything else is reduced to
    first letter + length, so the public page can never reveal an answer.
    """
    if not raw or not raw.get("rows"):
        return None
    rows = []
    for r in raw["rows"]:
        try:
            pct = round(float(r["pct"]), 1)
        except (KeyError, TypeError, ValueError):
            continue
        text = str(r.get("text", "")).strip()
        word = text.lower()
        if word in found:
            rows.append({"found": True, "word": word, "length": len(word), "pct": pct})
            continue
        m = _UNFOUND.match(text)
        if m:
            letter, length = m.group(1).upper(), int(m.group(2))
        elif word in answers:  # Buddy says found but we don't: don't reveal it
            letter, length = word[0].upper(), len(word)
        else:
            continue
        rows.append({"found": False, "letter": letter, "length": length, "pct": pct})

    # Words found since Buddy was last read still sit in an unfound row
    # ("L (6)"). If a word is the only new one with its letter and length, and
    # there's exactly one unfound row with that letter and length, that row is
    # its percentage, so show it as found straight away. Ambiguous cases wait
    # for the next Buddy read.
    named = {r["word"] for r in rows if r["found"]}
    new_by_key: dict[tuple, list[str]] = {}
    for w in found - named:
        new_by_key.setdefault((w[0].upper(), len(w)), []).append(w)
    rows_by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        if not r["found"]:
            rows_by_key.setdefault((r["letter"], r["length"]), []).append(r)
    for key, words in new_by_key.items():
        candidates = rows_by_key.get(key, [])
        if len(words) == 1 and len(candidates) == 1:
            r = candidates[0]
            pct = r["pct"]
            r.clear()
            r.update(found=True, word=words[0], length=key[1], pct=pct)
    rows.sort(key=lambda r: -r["pct"])
    sample = raw.get("sample")
    return {"sample": sample if isinstance(sample, int) else None, "rows": rows}


def build_public_data(
    *,
    answers: list[str],
    pangrams: list[str],
    center: str,
    outer: list[str],
    found: set[str],
    puzzle_id=None,
    print_date=None,
    logged_in: bool,
    visitors: dict | None = None,
) -> dict:
    answers_l = sorted({a.lower() for a in answers})
    answer_set = set(answers_l)
    pangram_set = {p.lower() for p in pangrams}
    found_l = {f.lower() for f in found} & answer_set
    letters = set(center.lower()) | {o.lower() for o in outer}
    perfect = {p for p in pangram_set if len(p) == 7 and set(p) == letters}

    lengths = sorted({len(a) for a in answers_l})
    first_letters = sorted({a[0] for a in answers_l})

    grid_total: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    grid_found: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    two_total: dict[str, int] = defaultdict(int)
    two_found: dict[str, int] = defaultdict(int)

    for w in answers_l:
        grid_total[w[0]][len(w)] += 1
        two_total[w[:2]] += 1
        if w in found_l:
            grid_found[w[0]][len(w)] += 1
            two_found[w[:2]] += 1

    def cell(letter, length):
        t = grid_total[letter][length]
        f = grid_found[letter][length]
        return {"total": t, "remaining": t - f}

    grid = {
        "lengths": lengths,
        "rows": [
            {
                "letter": letter,
                "cells": [cell(letter, n) for n in lengths],
                "total": sum(grid_total[letter].values()),
                "remaining": sum(grid_total[letter].values()) - sum(grid_found[letter].values()),
            }
            for letter in first_letters
        ],
        "column_totals": [
            {
                "total": sum(grid_total[l][n] for l in first_letters),
                "remaining": sum(grid_total[l][n] - grid_found[l][n] for l in first_letters),
            }
            for n in lengths
        ],
    }

    two_letter = []
    for letter in first_letters:
        two_letter.append(
            {
                "letter": letter,
                "items": [
                    {"prefix": p, "total": two_total[p], "remaining": two_total[p] - two_found[p]}
                    for p in sorted(two_total)
                    if p[0] == letter
                ],
            }
        )

    max_points = sum(word_score(w, pangram_set) for w in answers_l)
    points = sum(word_score(w, pangram_set) for w in found_l)

    return {
        "puzzle_id": puzzle_id,
        "print_date": print_date,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "logged_in": logged_in,
        "center": center.upper(),
        "outer": [o.upper() for o in outer],
        "totals": {
            "words": len(answers_l),
            "points": max_points,
            "pangrams": len(pangram_set),
            "perfect_pangrams": len(perfect),
        },
        "found": {
            "words": len(found_l),
            "points": points,
            "pangrams": len(found_l & pangram_set),
            "perfect_pangrams": len(found_l & perfect),
        },
        "rank": rank_for(points, max_points),
        "grid": grid,
        "two_letter": two_letter,
        "visitors": public_visitors(visitors, found_l, answer_set),
    }
