import json

from app.buddy import build_public_data, rank_for, word_score
from app.fake import GAME
from app.nyt import found_from_json
from app.session import parse_cookies_txt as _parse_cookies_txt


def make(found=()):
    return build_public_data(
        answers=GAME["answers"], pangrams=GAME["pangrams"], center=GAME["centerLetter"],
        outer=GAME["outerLetters"], found=set(found), puzzle_id=1, print_date="x", logged_in=True,
    )


def test_scoring():
    assert word_score("cant", set()) == 1
    assert word_score("talon", set()) == 5
    assert word_score("location", {"location"}) == 15


def test_no_answers_leak():
    blob = json.dumps(make(found=["cant"]))
    for w in GAME["answers"]:
        assert f'"{w}"' not in blob.lower()


def test_grid_and_two_letter_sum():
    d = make(found=["cant", "location"])
    assert d["totals"]["words"] == len(GAME["answers"])
    assert d["found"]["words"] == 2 and d["found"]["pangrams"] == 1
    assert sum(r["total"] for r in d["grid"]["rows"]) == d["totals"]["words"]
    assert sum(r["remaining"] for r in d["grid"]["rows"]) == d["totals"]["words"] - 2
    assert sum(c["remaining"] for c in d["grid"]["column_totals"]) == d["totals"]["words"] - 2
    two = sum(i["total"] for g in d["two_letter"] for i in g["items"])
    assert two == d["totals"]["words"]
    ca = next(i for g in d["two_letter"] for i in g["items"] if i["prefix"] == "ca")
    assert ca["remaining"] == ca["total"] - 1


def test_found_ignores_non_answers():
    d = make(found=["cant", "notaword"])
    assert d["found"]["words"] == 1


def test_rank():
    assert rank_for(0, 100)["name"] == "Beginner"
    assert rank_for(70, 100)["name"] == "Genius"
    assert rank_for(100, 100)["name"] == "Queen Bee"
    assert rank_for(69, 100)["points_to_next"] == 1


def test_found_from_json_picks_progress_not_pangrams():
    answers = set(GAME["answers"])
    bodies = [{"states": [{"game_data": {"answers": ["cant", "coal", "talon"],
                                          "pangrams": ["location"]}}]},
              {"answers": sorted(answers)}]
    assert found_from_json(bodies, answers, {"location"}) == {"cant", "coal", "talon"}


def test_cookies_txt(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("# Netscape\n#HttpOnly_.nytimes.com\tTRUE\t/\tTRUE\t1999999999\tNYT-S\tabc\n"
                 ".example.com\tTRUE\t/\tTRUE\t0\tx\ty\n")
    c = _parse_cookies_txt(p)
    assert len(c) == 1 and c[0]["name"] == "NYT-S" and c[0]["expires"] == 1999999999


def test_cookies_placeholder_ignored(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text(".nytimes.com\tTRUE\t/\tTRUE\t2000000000\tNYT-S\tPASTE_VALUE_HERE\n")
    assert _parse_cookies_txt(p) == []


def test_visitors_never_leak_unfound_words():
    raw = {"sample": 10000, "rows": [
        {"found": True, "text": "cant", "pct": 97.6},
        {"found": True, "text": "talon", "pct": 90},          # Buddy says found, we don't
        {"found": False, "text": "L (6)", "pct": 82.44},
        {"found": False, "text": "garbage", "pct": 10},
    ]}
    d = build_public_data(
        answers=GAME["answers"], pangrams=GAME["pangrams"], center=GAME["centerLetter"],
        outer=GAME["outerLetters"], found={"cant"}, logged_in=True, visitors=raw,
    )
    v = d["visitors"]
    assert v["sample"] == 10000
    assert v["rows"][0] == {"found": True, "word": "cant", "length": 4, "pct": 97.6}
    assert {"found": False, "letter": "T", "length": 5, "pct": 90.0} in v["rows"]
    assert {"found": False, "letter": "L", "length": 6, "pct": 82.4} in v["rows"]
    assert len(v["rows"]) == 3
    blob = json.dumps(d).lower()
    for w in GAME["answers"]:
        if w != "cant":
            assert f'"{w}"' not in blob


def test_perfect_pangram_counts():
    d = build_public_data(answers=["abcdefg", "abcdefga", "aaaa"], pangrams=["abcdefg", "abcdefga"],
                          center="a", outer=list("bcdefg"), found={"abcdefg"}, logged_in=True)
    assert d["totals"]["perfect_pangrams"] == 1
    assert d["found"]["pangrams"] == 1 and d["found"]["perfect_pangrams"] == 1


def _state(puzzle_id, words):
    return {"puzzle_id": str(puzzle_id), "game": "spelling_bee",
            "game_data": {"answers": words, "isRevealed": False, "rank": "x"}}


def test_found_from_json_queen_bee_all_words():
    # Regression: finding every word made the found list equal the answer list,
    # which the old heuristic skipped, so the page showed 0 found.
    answers = set(GAME["answers"])
    bodies = [{"states": [_state(1, ["cant"]), _state(99999, sorted(answers))]}]
    assert found_from_json(bodies, answers, {"location"}, 99999) == answers


def test_found_from_json_matches_todays_puzzle_only():
    answers = set(GAME["answers"])
    bodies = [_state(1, ["cant", "coal", "talon", "tonal"]), _state(99999, ["cant"])]
    assert found_from_json(bodies, answers, {"location"}, 99999) == {"cant"}


def test_found_from_json_only_pangrams_found():
    answers = set(GAME["answers"])
    bodies = [_state(99999, ["location"])]
    assert found_from_json(bodies, answers, {"location"}, 99999) == {"location"}


def _visitors(found, rows):
    return build_public_data(
        answers=GAME["answers"], pangrams=GAME["pangrams"], center=GAME["centerLetter"],
        outer=GAME["outerLetters"], found=set(found), logged_in=True,
        visitors={"sample": 10000, "rows": rows},
    )["visitors"]["rows"]


def test_visitors_new_word_fills_unique_row_immediately():
    # Buddy was read when only "cant" was found; "talon" is the only T (5).
    rows = [{"found": True, "text": "cant", "pct": 97.6},
            {"found": False, "text": "T (5)", "pct": 80.2},
            {"found": False, "text": "C (5)", "pct": 70.0},
            {"found": False, "text": "C (5)", "pct": 60.0}]
    out = _visitors({"cant", "talon"}, rows)
    assert {"found": True, "word": "talon", "length": 5, "pct": 80.2} in out
    assert sum(r["found"] for r in out) == 2 and len(out) == 4


def test_visitors_ambiguous_new_word_waits_for_next_read():
    # "canal" and "coala"... two unfound C (5) rows: can't tell which is canal's.
    rows = [{"found": False, "text": "C (5)", "pct": 70.0},
            {"found": False, "text": "C (5)", "pct": 60.0}]
    out = _visitors({"canal"}, rows)
    assert not any(r["found"] for r in out)
    assert "canal" not in json.dumps(out)


def test_visitors_from_stats_file():
    from app.buddy import visitors_from_stats
    stats = {"id": 1, "answers": {"cant": 9765, "talon": 8244, "location": 4000}, "n": 10000, "numberOfUsers": 71456}
    raw = visitors_from_stats(stats)
    assert raw["sample"] == 10000 and len(raw["rows"]) == 3
    d = build_public_data(
        answers=GAME["answers"], pangrams=GAME["pangrams"], center=GAME["centerLetter"],
        outer=GAME["outerLetters"], found={"cant"}, logged_in=True, visitors=raw,
    )
    rows = d["visitors"]["rows"]
    assert rows[0] == {"found": True, "word": "cant", "length": 4, "pct": 97.7}
    assert {"found": False, "letter": "T", "length": 5, "pct": 82.4} in rows
    assert {"found": False, "letter": "L", "length": 8, "pct": 40.0} in rows
    blob = json.dumps(d)
    assert '"talon"' not in blob and '"location"' not in blob


def test_visitors_from_stats_rejects_bad_files():
    from app.buddy import visitors_from_stats
    assert visitors_from_stats({}) is None
    assert visitors_from_stats({"answers": {}, "n": 10000}) is None
    assert visitors_from_stats({"answers": {"cant": 1}, "n": 0}) is None
