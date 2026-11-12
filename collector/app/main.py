"""Collector API.

The public nginx server forwards only these two:
  POST /sync    start a sync unless one is running or the data is fresh enough
  GET  /status  current sync state (safe to show publicly)

The admin page is at / (its API is under /admin/). docker-compose publishes
this port on 127.0.0.1 only, so it's reachable from the Docker host only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .buddy import build_public_data
from .session import save_admin_cookie, valid_cookie_value

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("collector")

PUBLIC_DIR = Path(os.environ.get("PUBLIC_DIR", "/public"))
MIN_SYNC_INTERVAL = int(os.environ.get("MIN_SYNC_INTERVAL", "5"))       # seconds
BACKGROUND_SYNC_MINUTES = int(os.environ.get("BACKGROUND_SYNC_MINUTES", "60"))
FAKE_NYT = os.environ.get("FAKE_NYT", "0") == "1"  # local testing without NYT
ADMIN_HTML = Path(__file__).parent / "admin.html"

# Buddy's visitor stats are slow to read, so they refresh in the background:
# after your found words change (at most once per VISITOR_STATS_MIN_GAP), and
# at least every VISITOR_STATS_MINUTES.
VISITOR_STATS = os.environ.get("VISITOR_STATS", "1") == "1"
VISITOR_STATS_MAX_AGE = int(os.environ.get("VISITOR_STATS_MINUTES", "15")) * 60
VISITOR_STATS_MIN_GAP = 60

if FAKE_NYT:
    from .fake import FakeNYT as Client
else:
    from .nyt import NYT as Client

nyt = Client()


class SyncState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.last_finished = 0.0
        self.last_ok = 0.0
        self.status = "idle"      # idle | running | ok | needs_login | error
        self.message = ""
        self.rerun = False        # a new cookie arrived mid-sync; sync again after
        self.snap = None          # last successful Snapshot (private)
        self.visitors = None      # last visitor stats (private, raw)
        self.visitors_key = None  # (puzzle id, found words) they were read for
        self.visitors_at = 0.0
        self.visitors_task: asyncio.Task | None = None

    def public(self) -> dict:
        return {
            "status": self.status,
            "running": bool(self.task and not self.task.done()),
            "message": self.message,
            "last_ok": int(self.last_ok) or None,
            "min_interval": MIN_SYNC_INTERVAL,
        }


state = SyncState()


def _write_public(data: dict) -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PUBLIC_DIR / "buddy.json.tmp"
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.chmod(0o644)
    tmp.replace(PUBLIC_DIR / "buddy.json")  # atomic swap, nginx never sees half a file


def _publish() -> dict:
    """Write buddy.json from the latest snapshot and visitor stats."""
    snap = state.snap
    g = snap.game
    visitors = state.visitors if state.visitors_key and state.visitors_key[0] == g.get("id") else None
    data = build_public_data(
        answers=g["answers"],
        pangrams=g.get("pangrams", []),
        center=g["centerLetter"],
        outer=g["outerLetters"],
        found=snap.found,
        puzzle_id=g.get("id"),
        print_date=g.get("printDate"),
        logged_in=snap.logged_in,
        visitors=visitors if snap.logged_in else None,
    )
    _write_public(data)
    return data


async def _refresh_visitors() -> None:
    """Read Buddy's visitor stats, waiting out the minimum gap first."""
    wait = VISITOR_STATS_MIN_GAP - (time.time() - state.visitors_at)
    if wait > 0:
        await asyncio.sleep(wait)
    snap = state.snap
    key = (snap.game.get("id"), frozenset(snap.found))  # read for the latest words
    try:
        data = await nyt.visitor_stats()
    except Exception:
        log.exception("Visitor stats failed")
        data = None
    state.visitors_at = time.time()
    if data:
        state.visitors, state.visitors_key = data, key
        async with state.lock:   # don't write while a sync is mid-write
            _publish()


def _maybe_refresh_visitors() -> None:
    snap = state.snap
    if not (VISITOR_STATS and snap and snap.logged_in):
        return
    if state.visitors_task and not state.visitors_task.done():
        return
    key = (snap.game.get("id"), frozenset(snap.found))
    stale = time.time() - state.visitors_at > VISITOR_STATS_MAX_AGE
    if key != state.visitors_key or stale:
        state.visitors_task = asyncio.create_task(_refresh_visitors())


async def _run_sync() -> None:
    async with state.lock:
        state.status, state.message = "running", ""
        started = time.monotonic()
        try:
            snap = await nyt.snapshot()
            state.snap = snap
            data = _publish()
            state.last_ok = time.time()
            if snap.logged_in:
                state.status = "ok"
            else:
                state.status = "needs_login"
                state.message = "Not logged in to NYT; showing puzzle totals only."
            log.info("Sync ok in %.1fs: %d/%d words (source=%s, logged_in=%s)",
                     time.monotonic() - started, data["found"]["words"], data["totals"]["words"],
                     snap.found_source, snap.logged_in)
        except Exception:
            # Details stay in the container log, never in the public status.
            log.exception("Sync failed")
            state.status, state.message = "error", "Sync failed; showing last good data."
        finally:
            state.last_finished = time.time()
    _maybe_refresh_visitors()
    if state.rerun:
        state.rerun = False
        state.task = asyncio.create_task(_run_sync())


def request_sync(force: bool = False) -> str:
    if state.task and not state.task.done():
        return "running"
    if not force and time.time() - state.last_finished < MIN_SYNC_INTERVAL:
        return "fresh"
    state.task = asyncio.create_task(_run_sync())
    return "started"


async def _background_loop():
    while True:
        request_sync(force=True)
        await asyncio.sleep(max(BACKGROUND_SYNC_MINUTES, 1) * 60)


@asynccontextmanager
async def lifespan(_app):
    bg = asyncio.create_task(_background_loop()) if BACKGROUND_SYNC_MINUTES > 0 else None
    if bg is None:
        request_sync(force=True)
    yield
    if bg:
        bg.cancel()
    await nyt.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/sync", status_code=202)
async def sync():
    return {"result": request_sync(), **state.public()}


@app.get("/status")
async def status():
    return state.public()


# ---------- admin (localhost only) ----------

class CookieIn(BaseModel):
    value: str


def _public_data() -> dict | None:
    try:
        return json.loads((PUBLIC_DIR / "buddy.json").read_text())
    except Exception:
        return None


@app.get("/", include_in_schema=False)
@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(ADMIN_HTML, headers={"Cache-Control": "no-store"})


@app.get("/admin/state")
async def admin_state():
    data = _public_data()
    return {
        **state.public(),
        "logged_in": data.get("logged_in") if data else None,
        "updated_at": data.get("updated_at") if data else None,
        "found_words": data["found"]["words"] if data else None,
        "total_words": data["totals"]["words"] if data else None,
        "rank": data["rank"]["name"] if data else None,
        "fake": FAKE_NYT,
    }


@app.post("/admin/cookie")
async def admin_cookie(body: CookieIn):
    value = body.value.strip().strip('"')
    if not valid_cookie_value(value):
        raise HTTPException(400, "That doesn't look like an NYT-S cookie value.")
    save_admin_cookie(value)
    log.info("NYT cookie updated from the admin page")
    if request_sync(force=True) == "running":
        state.rerun = True
    return {"ok": True}


@app.post("/admin/sync")
async def admin_sync():
    return {"result": request_sync(force=True)}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
