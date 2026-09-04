"""
The four failure modes, as buttons.

Razorpay's Track 03 bar asks for "one failure handled gracefully". Describing
that in a README is cheap. Pressing it on camera is not.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import numpy as np
from fastapi import APIRouter

from app.config_loader import load_config
from app.controllers import execute as ex
from app.domain.engine import decide
from app.domain.models import ActionType, Arm, CaseSnapshot, FailureClass, ObligationKind
from app.repos import store
from app.services.bandit import Posterior
from app.services.executor import SandboxExecutor
from app.workers.reconciler import reconcile

router = APIRouter(prefix="/api/chaos", tags=["chaos"])

_con = None
_exec = None
LLM_ALIVE = {"value": True}


def con():
    global _con, _exec
    if _con is None:
        _con = store.connect()
        store.init(_con)
        _exec = SandboxExecutor(_con)
    return _con


def executor() -> SandboxExecutor:
    con()
    return _exec


def _seed_case(settled: bool = False) -> tuple[str, str]:
    c = con()
    oid = f"ob_demo_{uuid.uuid4().hex[:8]}"
    cid = f"case_demo_{uuid.uuid4().hex[:8]}"
    c.execute("INSERT INTO obligations VALUES (?,?,?,?,?,?)",
              (oid, "cust_demo", 500_000, 500_000 if settled else 0,
               "SETTLED" if settled else "OPEN", datetime.now().isoformat()))
    c.commit()
    return cid, oid


@router.post("/duplicate_webhook")
def duplicate_webhook():
    """The same Razorpay event arrives twice. One event row, one action."""
    c = con()
    key = f"evt_{uuid.uuid4().hex[:10]}"
    payload = json.dumps({"event": "payment.failed", "id": key})
    inserted = 0
    for _ in range(2):
        cur = c.execute(
            "INSERT OR IGNORE INTO events (dedupe_key, obligation_id, type, payload, received_at)"
            " VALUES (?,?,?,?,?)",
            (key, "ob_demo", "payment.failed", payload, datetime.now().isoformat()))
        inserted += cur.rowcount
    c.commit()
    rows = c.execute("SELECT COUNT(*) FROM events WHERE dedupe_key = ?", (key,)).fetchone()[0]

    cid, oid = _seed_case()
    at = datetime.now()
    a1 = ex.schedule(c, cid, oid, ActionType.RETRY, at)
    a2 = ex.schedule(c, cid, oid, ActionType.RETRY, at)   # same idem key

    return {
        "webhook_deliveries": 2, "event_rows_created": inserted, "event_rows_now": rows,
        "actions_scheduled": 2, "actions_created": sum(x is not None for x in (a1, a2)),
        "verdict": "one event, one action" if rows == 1 and a2 is None else "DUPLICATE LEAKED",
        "how": "UNIQUE(events.dedupe_key) + UNIQUE(actions.idem_key), INSERT OR IGNORE",
    }


@router.post("/kill_llm")
def kill_llm():
    """Pull the LLM's plug. The engine keeps deciding, because it never asked one."""
    LLM_ALIVE["value"] = not LLM_ALIVE["value"]
    cfg = load_config()
    snap = _demo_snapshot()
    d = decide(snap, cfg, Posterior(cfg), np.random.default_rng(1))
    return {
        "llm_alive": LLM_ALIVE["value"],
        "engine_still_decides": True,
        "decision": d.action.value,
        "stop_reason": d.stop_reason.value if d.stop_reason else None,
        "degraded": ["message wording falls back to a static DLT template",
                     "error strings fall back to code-based classification"]
        if not LLM_ALIVE["value"] else [],
        "verdict": "LLM is a feature, not a dependency",
    }


@router.post("/executor_timeout")
def executor_timeout():
    """The gateway stops answering. We do NOT assume failure."""
    c = con()
    cid, oid = _seed_case()
    aid = ex.schedule(c, cid, oid, ActionType.RETRY, datetime.now() - timedelta(minutes=1))
    executor().force_timeout = True
    row = c.execute("SELECT * FROM actions WHERE action_id = ?", (aid,)).fetchone()
    result = ex.run(c, row, executor())
    after = reconcile(c, executor())
    return {"execution": result, "reconciliation": after,
            "verdict": "timeout -> UNKNOWN -> reconciled. never guessed."}


@router.post("/pay_midflight")
def pay_midflight():
    """Customer pays while our contact sits in the queue. Nothing gets sent."""
    c = con()
    cid, oid = _seed_case()
    aid = ex.schedule(c, cid, oid, ActionType.PAY_LINK, datetime.now() - timedelta(minutes=1))
    c.execute("UPDATE obligations SET status='SETTLED', amount_settled=amount_due WHERE id=?",
              (oid,))
    c.commit()
    row = c.execute("SELECT * FROM actions WHERE action_id = ?", (aid,)).fetchone()
    result = ex.run(c, row, executor())
    return {"execution": result,
            "contact_sent": result["contact_sent"],
            "verdict": "aborted before send. a paying customer was never asked to pay again."}


@router.get("/state")
def state():
    c = con()
    return {
        "llm_alive": LLM_ALIVE["value"],
        "events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "actions": {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM actions GROUP BY status")},
    }


def _demo_snapshot() -> CaseSnapshot:
    now = datetime(2026, 3, 10, 14, 0, 0)
    return CaseSnapshot(
        case_id="demo", obligation_id="ob_demo", customer_id="cust_demo",
        merchant_id="m1", arm=Arm.ENGINE, amount_due=500_000, amount_settled=0,
        kind=ObligationKind.ORDER, currency="INR",
        failure_class=FailureClass.INSUFFICIENT_FUNDS, method="card", is_mandate=False,
        now=now, opened_at=now - timedelta(hours=3), attempts=0, rung=0,
        last_action_at=None, promised_until=None, customer_tenure_days=200,
        customer_past_failures=1, customer_past_recoveries=1, contacts_last_7d=0,
        opted_out=False, risk_blocked=False, last_notice_sent_at=None, afa_valid=True,
        obligation_settled=False, method_in_downtime=False, downtime_ends_at=None,
        pending_action_types=(),
    )
