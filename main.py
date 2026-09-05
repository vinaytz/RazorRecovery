#!/usr/bin/env python3
"""FastAPI entrypoint. Serves the dashboard and the benchmark results.

Run:  python main.py     ->  http://localhost:8000
"""
from __future__ import annotations

import json
from pathlib import Path

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("razorrecovery")

# The live loop. One second, because at TIME_SCALE=3600 a six-hour wait becomes six
# seconds and a slower tick would be visible as a stutter in a filmed demo. Both
# passes are idempotent and indexed, so an idle tick is a couple of queries.
TICK_SECONDS = 1.0

# The abandonment sweep is far coarser: it enforces a 30-minute window, so a minute
# of latency is noise. Running it every tick would be a wasted query 59 times out
# of 60.
SWEEP_EVERY = 60


async def _live_loop() -> None:
    """Decide, execute, sweep. The live counterpart of the benchmark's tick loop.

    Runs in a thread per tick because everything under it is blocking sqlite and
    requests. One tick at a time by construction -- no overlap, so no two ticks can
    both decide on the same case.
    """
    from datetime import datetime

    from app.api import dashboard
    from app.services.executor import build_executor
    from app.workers import sweeper
    from app.workers.live import LiveWorker

    con = dashboard.con()
    worker = LiveWorker(con, build_executor(con=con))
    log.info("live worker started: tick=%ss time_scale=%s abandon_minutes=%s",
             TICK_SECONDS, live_mod_scale(), sweeper.abandon_minutes())

    ticks = 0
    while True:
        await asyncio.sleep(TICK_SECONDS)
        ticks += 1
        try:
            out = await asyncio.to_thread(worker.tick, datetime.now())
            if out["decided"] or out["executed"]:
                log.info("tick: %s", out["verdict"])
        except Exception:
            # A worker that dies silently is worse than one that logs and retries.
            # If this loop stops, cases sit OPEN forever and the demo shows nothing.
            log.exception("live tick failed -- retrying on the next tick")

        if ticks % SWEEP_EVERY == 0:
            try:
                swept = await asyncio.to_thread(sweeper.sweep, con, datetime.now())
                if swept["opened"]:
                    log.info("sweeper: %s", swept["verdict"])
            except Exception:
                log.exception("sweep failed -- retrying on the next sweep")


def live_mod_scale() -> float:
    from app.workers.live import time_scale
    return time_scale()


def _worker_enabled() -> bool:
    """`WORKER=off` disables the loop. On by default: a recovery engine whose worker
    is opt-in is a recovery engine that does nothing on a fresh clone."""
    return os.environ.get("WORKER", "on").strip().lower() not in ("off", "0", "false")


@contextlib.asynccontextmanager
async def lifespan(app_: FastAPI):
    task = asyncio.create_task(_live_loop()) if _worker_enabled() else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="RazorRecovery", lifespan=lifespan)

from app.api import chaos, checkout, dashboard, feed, ops, replay, webhooks  # noqa: E402
app.include_router(dashboard.router)
app.include_router(replay.router)
app.include_router(chaos.router)
app.include_router(webhooks.router)
app.include_router(ops.router)
app.include_router(feed.router)
app.include_router(checkout.router)


def _results() -> dict:
    p = ROOT / "results.json"
    if not p.exists():
        raise HTTPException(404, "no results yet -- run: python run_benchmark.py")
    return json.loads(p.read_text())


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    p = ROOT / "web" / "index.html"
    if not p.exists():
        return JSONResponse({"msg": "dashboard not built yet (TASKS.md P5)",
                             "try": "/api/scoreboard"})
    return FileResponse(p)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
