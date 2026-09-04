"""Read API for the dashboard. All numbers come from a completed benchmark run."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.controllers import ingest as ingest_ctl
from app.repos import store
from app.services import matcher

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


# -- the live settlement ledger -------------------------------------------
# Everything below is the LIVE path, not the benchmark. A simulated case never
# has to work out which debt a payment belongs to; a real one always does.

@router.get("/settlements")
def settlements(limit: int = 100):
    """Recovered money, and how sure we are that it belongs to what we closed.

    The distribution matters more than the total. `pct_certain` is the share of
    recovered rupees matched on an exact id; the rest was matched on a contact, an
    amount, or somebody's word, and the dashboard shows it that way.
    """
    c = con()
    return {"distribution": store.match_distribution(c),
            "ladder": {str(k): {"basis": v[0], "confidence": v[1], "means": v[2]}
                       for k, v in matcher.LADDER.items()},
            "unmatched": store.unmatched_settlements(c),
            "settlements": store.list_settlements(c, limit)}


@router.post("/cases/{case_id}/settled")
def settled_out_of_band(case_id: str, amount: int | None = None,
                        who: str | None = None, reference: str | None = None,
                        note: str | None = None):
    """Level 5 of the match ladder: cash, bank transfer, a cheque in the post.

    There is no webhook for money that never touched Razorpay, so a human records
    it here. The case closes and the customer stops being chased -- but the row
    says `asserted`, not `certain`, because we did not observe this money.
    """
    out = ingest_ctl.settle_from_ledger(con(), case_id, datetime.now(), amount=amount,
                                       who=who, reference=reference, note=note)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error", "could not settle"))
    return out

