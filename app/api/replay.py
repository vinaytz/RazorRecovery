"""
Replay: re-run a stored decision and prove it lands in the same place.

Only possible because engine.decide() never touched a database. The stored
snapshot IS the input, so replaying is not a simulation of the past -- it is the
past, run again.
"""
from __future__ import annotations

import numpy as np
from fastapi import APIRouter, HTTPException

from app.config_loader import load_config, version as config_version
from app.domain.engine import decide
from app.domain.models import CaseSnapshot
from app.repos import store
from app.services.bandit import FrozenPosterior

router = APIRouter(prefix="/api", tags=["replay"])
_con = None


def con():
    global _con
    if _con is None:
        _con = store.connect()
        store.init(_con)
    return _con


@router.get("/replay/{decision_id}")
def replay(decision_id: str):
    d = store.get_decision(con(), decision_id)
    if not d:
        raise HTTPException(404, "no such decision")

    snap = CaseSnapshot.from_json(d["snapshot"])
    cfg = config_version(d["config_version"]) or load_config()
    posterior = FrozenPosterior(d["candidates"])
    out = decide(snap, cfg, posterior, np.random.default_rng(0))

    return {
        "decision_id": decision_id,
        "then": {"action": d["action"], "stop_reason": d["stop_reason"]},
        "now": {"action": out.action.value,
                "stop_reason": out.stop_reason.value if out.stop_reason else None},
        "match": out.action.value == d["action"],
        "snapshot": d["snapshot"],
        "gate_trace": d["gate_trace"],
        "candidates": d["candidates"],
        "notes": d["notes"],
    }
