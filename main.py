#!/usr/bin/env python3
"""FastAPI entrypoint. Serves the dashboard and the benchmark results.

Run:  python main.py     ->  http://localhost:8000
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="RazorRecovery")

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
