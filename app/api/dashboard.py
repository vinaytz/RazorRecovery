"""Read API for the dashboard. All numbers come from a completed benchmark run."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.repos import store

router = APIRouter(prefix="/api", tags=["dashboard"])
_con = None


def con():
    global _con
    if _con is None:
        _con = store.connect()
        store.init(_con)
    return _con


@router.get("/runs")
def runs():
    return {"runs": store.list_runs(con())}


@router.get("/scoreboard")
def scoreboard(run_id: str = "default"):
    r = store.get_run(con(), run_id)
    if not r:
        raise HTTPException(404, f"no run '{run_id}'. run: python run_benchmark.py")
    return json.loads(r["board"])


@router.get("/cases")
def cases(run_id: str = "default", arm: str | None = None, status: str | None = None,
          limit: int = 60, offset: int = 0):
    return {"cases": store.list_cases(con(), run_id, arm, status, limit, offset)}


@router.get("/case/{case_id}")
def case(case_id: str):
    c = store.get_case(con(), case_id)
    if not c:
        raise HTTPException(404, "no such case")
    return c


@router.get("/highlights")
def highlights(run_id: str = "default", kind: str = "sleeping_dog", limit: int = 20):
    """The decisions worth putting on camera, found automatically."""
    return {"kind": kind, "decisions": store.highlights(con(), run_id, kind, limit)}
