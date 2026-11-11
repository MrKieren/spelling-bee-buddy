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
MIN_SYNC_INTERVAL = int(os.environ.get("MIN_SYNC_INTERVAL", "30"))      # seconds
BACKGROUND_SYNC_MINUTES = int(os.environ.get("BACKGROUND_SYNC_MINUTES", "60"))
FAKE_NYT = os.environ.get("FAKE_NYT", "0") == "1"  # local testing without NYT
ADMIN_HTML = Path(__file__).parent / "admin.html"

if FAKE_NYT:
    from .fake import snapshot  # noqa: F401
else:
    from .nyt import snapshot  # noqa: F401


class SyncState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.last_finished = 0.0
        self.last_ok = 0.0
        self.status = "idle"      # idle | running | ok | needs_login | error
        self.message = ""
        self.rerun = False        # a new cookie arrived mid-sync; sync again after

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


async def _run_sync() -> None:
    async with state.lock:
        state.status, state.message = "running", ""
        started = time.monotonic()
        try:
            snap = await snapshot()
            g = snap.game
            data = build_public_data(
                answers=g["answers"],
                pangrams=g.get("pangrams", []),
                center=g["centerLetter"],
                outer=g["outerLetters"],
                found=snap.found,
                puzzle_id=g.get("id"),
                print_date=g.get("printDate"),
                logged_in=snap.logged_in,
                visitors=snap.visitors,
            )
            _write_public(data)
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
