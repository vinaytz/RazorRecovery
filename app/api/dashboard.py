"""Read API for the dashboard. All numbers come from a completed benchmark run."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from app.api import webhooks
from app.controllers import ingest as ingest_ctl
from app.repos import store
from app.services import matcher
from app.workers import live as live_worker
from app.workers import sweeper

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


@router.post("/demo/fail")
def demo_fail(name: str = "01_payment_failed_insufficient_funds.json",
              amount: int | None = None, email: str | None = None,
              contact: str | None = None):
    """Seed one failed payment from a fixture. Works with no tunnel and no keys.

    This exists because a demo cannot depend on a tunnel staying up, and a judge
    cannot be asked to make a real payment fail. It pushes a saved payload through
    the SAME ingest path a Razorpay webhook takes -- signature check included, via
    the replay route's own signing -- so what gets demoed is the real handler.

    `amount`, `email` and `contact` are overrides so a filmed run can produce a
    case that is worth watching (a large one, reaching a real inbox) without
    editing a fixture on disk.
    """
    path = webhooks.FIXTURES / name
    if not path.exists():
        raise HTTPException(404, f"no fixture '{name}'. see GET /webhooks/fixtures")

    payload = json.loads(path.read_text())
    ent = ((payload.get("payload") or {}).get("payment") or {}).get("entity")
    if not isinstance(ent, dict):
        raise HTTPException(400, f"'{name}' carries no payment entity to fail")

    # A fresh id per call, or the second demo run is deduped as a retry and the
    # judge watches nothing happen.
    stamp = datetime.now().strftime("%H%M%S%f")[:10]
    oid = f"order_DEMO{stamp}"
    ent["id"] = f"pay_DEMO{stamp}"
    ent["order_id"] = oid
    payload["id"] = f"evt_DEMO{stamp}"
    if amount is not None:
        ent["amount"] = int(amount)
    if email:
        ent["email"] = email
    if contact:
        ent["contact"] = contact

    out = ingest_ctl.ingest(con(), payload, now=datetime.now())
    out["seeded_from"] = name
    out["obligation_id"] = oid
    out["next"] = ("POST /api/worker/tick to decide on it, or wait for the "
                   "background worker")
    return out


@router.post("/worker/tick")
def worker_tick():
    """Run one live decide-and-execute pass by hand. The loop does this on a timer."""
    from app.services.executor import build_executor
    from app.workers.live import LiveWorker

    c = con()
    return LiveWorker(c, build_executor(con=c)).tick(datetime.now())


@router.get("/live")
def live_state():
    """What the live path is doing right now: cases, actions, contacts, decisions."""
    c = con()
    return {
        "time_scale": live_worker.time_scale(),
        "abandon_minutes": sweeper.abandon_minutes(),
        "cases": [dict(r) for r in c.execute(
            "SELECT * FROM cases WHERE run_id = 'live' ORDER BY opened_at DESC LIMIT 40")],
        "actions": [dict(r) for r in c.execute(
            "SELECT * FROM actions ORDER BY created_at DESC LIMIT 40")],
        "contacts": [dict(r) for r in c.execute(
            "SELECT * FROM contacts ORDER BY sent_at DESC LIMIT 40")],
        "decisions": [dict(r) for r in c.execute(
            "SELECT decision_id, case_id, decided_at, action, stop_reason, notes"
            " FROM decisions WHERE run_id = 'live' ORDER BY decided_at DESC LIMIT 40")],
    }


@router.get("/checkouts")
def checkouts():
    """The abandonment watch list. What is being watched, and what it became.

    WATCHING is not at-risk revenue yet -- most of it will be paid in the next
    minute. Only ABANDONED has become a case.
    """
    c = con()
    window = sweeper.abandon_minutes()
    cutoff = (datetime.now() - timedelta(minutes=window)).isoformat()
    return {
        "abandon_minutes": window,
        "counts": store.checkout_counts(c),
        "due_now": store.due_checkouts(c, cutoff),
        "watching": [dict(r) for r in c.execute(
            "SELECT * FROM checkouts WHERE status = 'WATCHING' ORDER BY created_at DESC"
            " LIMIT 50")],
        "abandoned": [dict(r) for r in c.execute(
            "SELECT * FROM checkouts WHERE status = 'ABANDONED'"
            " ORDER BY resolved_at DESC LIMIT 50")],
        "note": ("an abandoned checkout is an ABSENCE, not an event -- there is no "
                 "webhook for closing a tab, so the sweeper looks for the payment "
                 "that never arrived"),
    }


@router.post("/sweep")
def sweep_now(minutes: int | None = None):
    """Run the abandonment sweep. The worker calls this on a timer; the demo calls
    it by hand with a shorter `minutes` so a filmed run does not take half an hour."""
    return sweeper.sweep(con(), datetime.now(), minutes=minutes)

