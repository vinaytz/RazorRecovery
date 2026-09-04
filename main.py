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

# The abandonment sweep runs on a timer because the thing it looks for is an
# absence -- no webhook will ever wake it up. 60s is far finer than the 30-minute
# window it enforces, so the extra latency is noise, and the sweep is idempotent,
# so a tick that finds nothing costs one indexed query.
SWEEP_SECONDS = 60


async def _sweep_loop() -> None:
    from datetime import datetime

    from app.api import dashboard
    from app.workers import sweeper

    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            out = await asyncio.to_thread(sweeper.sweep, dashboard.con(), datetime.now())
            if out["opened"]:
                log.info("sweeper: %s", out["verdict"])
        except Exception:
            # A worker that dies silently is worse than one that logs and retries:
            # abandoned checkouts would simply stop being noticed.
            log.exception("sweep tick failed -- retrying on the next tick")


@contextlib.asynccontextmanager
async def lifespan(app_: FastAPI):
    task = None
    if os.environ.get("SWEEPER", "on").strip().lower() not in ("off", "0", "false"):
        task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="RazorRecovery", lifespan=lifespan)

from app.api import chaos, dashboard, replay, webhooks  # noqa: E402
app.include_router(dashboard.router)
app.include_router(replay.router)
app.include_router(chaos.router)
app.include_router(webhooks.router)


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
