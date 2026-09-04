"""Capture the before-shot for item 3a: the engine spending a rung on a RETRY that
cannot possibly work.

Writes docs/evidence/pre_3a_wasted_rung.txt. Run before the G8 change lands; after
3a the same script should show G8 blocking RETRY and the ladder going straight to
PAY_LINK.
"""
import io
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["DRY_RUN"] = "false"          # dry run short-circuits before the RETRY branch
os.environ["TIME_SCALE"] = "7200"

from app.controllers import ingest as ingest_ctl          # noqa: E402
from app.repos import store                               # noqa: E402
from app.services.executor import build_executor          # noqa: E402
from app.services.llm import get_llm                      # noqa: E402
from app.workers.live import LiveWorker                   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "pre_3a_wasted_rung.txt"
DB = "/tmp/rr_pre3a.db"
OID, CUST = "order_PRE3A", "cust_PRE3A"

logbuf = io.StringIO()
h = logging.StreamHandler(logbuf)
h.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
logging.getLogger().addHandler(h)
logging.getLogger().setLevel(logging.INFO)


class OffsetClock:
    """Real time shifted into the contact window so quiet hours is not the story."""

    def __init__(self, hours):
        self.delta = timedelta(hours=hours)

    def now(self):
        return datetime.now() + self.delta


def run() -> str:
    if os.path.exists(DB):
        os.remove(DB)
    now = datetime.now()
    # Land at 12:00 local whatever time this is actually run.
    clock = OffsetClock(12 - now.hour - now.minute / 60)
    con = store.connect(DB)
    store.init(con)
    w = LiveWorker(con, build_executor(con=con, clock=clock))

    ingest_ctl.ingest(con, {
        "entity": "event", "event": "payment.failed", "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": "pay_PRE3A", "entity": "payment", "amount": 1_250_000,
            "currency": "INR", "status": "failed", "order_id": OID,
            "method": "card", "customer_id": CUST,
            "email": "asha@example.com", "contact": "+919000000001",
            "notes": {"name": "Asha Menon"},
            "error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_failed",
            "error_description": "Your card has insufficient balance.",
            "created_at": int(time.time())}}},
        "created_at": int(time.time()), "id": "evt_PRE3A",
    }, {}, llm=get_llm())

    cid = f"live_{OID}"
    out = io.StringIO()
    p = out.write

    snap = w.snapshot(con.execute("SELECT * FROM cases WHERE case_id=?",
                                  (cid,)).fetchone(), clock.now())
    p("WHAT THE ENGINE IS LOOKING AT\n")
    p(f"  case            {snap.case_id}\n")
    p(f"  kind            {snap.kind.value}\n")
    p(f"  is_mandate      {snap.is_mandate}\n")
    p(f"  failure_class   {snap.failure_class.value}\n")
    p(f"  amount          {snap.amount_remaining} paise\n\n")
    p("  A one-time order and no mandate. We hold no instrument, so there is\n")
    p("  nothing on file to charge. RETRY is not a lever here -- it is a no-op\n")
    p("  the ladder has to climb past.\n\n")

    for _ in range(200):
        tick = w.tick(clock.now())
        for d in tick["decided"]:
            if d.get("action") == "RETRY":
                p("WHAT THE GATES SAY\n")
                row = con.execute("SELECT gate_trace FROM decisions WHERE case_id=?"
                                  " ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()
                import json
                for g in json.loads(row[0]):
                    mark = "pass " if g["passed"] else "BLOCK"
                    star = "   <-- passes. nothing here knows RETRY is impossible." \
                        if g["gate"] == "G8_NON_RETRYABLE" else ""
                    p(f"  {g['gate']:<20}{mark} {g['detail']}{star}\n")
                p(f"\n  -> the engine chooses RETRY. rung 0 -> 1.\n\n")
        if any(a.get("type") == "RETRY" for a in tick["executed"]):
            break
        time.sleep(0.2)

    p("WHAT ACTUALLY HAPPENS WHEN IT RUNS\n")
    for r in con.execute("SELECT type, status, detail FROM actions WHERE case_id=?"
                         " ORDER BY rowid", (cid,)):
        p(f"  {r[0]:<12}{r[1]:<10}{r[2] or ''}\n")
    rung, attempts, contacts = con.execute(
        "SELECT rung, attempts, contacts_sent FROM cases WHERE case_id=?",
        (cid,)).fetchone()
    p(f"\n  rung {rung}   attempts {attempts}   contacts_sent {contacts}\n")
    p("\n  The rung was spent. The customer heard nothing. The only thing that\n")
    p("  moved is the ladder position -- which now sits one rung closer to\n")
    p("  WRITE_OFF than it did before, having bought nothing.\n\n")

    p("WHERE THAT STRING SURFACES\n")
    p("  There is no log line. `app/controllers/execute.py` does not log at all,\n")
    p("  so a rung burned on an impossible RETRY is silent to an operator today.\n")
    p("  The string exists in exactly two places:\n\n")
    row = con.execute("SELECT action_id, type, status, detail, attempts FROM actions"
                      " WHERE case_id=? AND type='RETRY'", (cid,)).fetchone()
    p(f"  1. actions.detail       {row[0]}  {row[1]}  {row[2]}\n")
    p(f"                          {row[3]!r}\n")
    p("  2. the JSON returned by POST /api/worker/tick, under executed[].detail\n\n")
    for line in logbuf.getvalue().splitlines():
        if "INTENT_ONLY" in line:
            p(f"  log: {line}\n")
    p("  executor.py:481 -- RazorpayExecutor never calls a charge API. See its\n")
    p("  class docstring: creating a payment link is reversible and rate-limited;\n")
    p("  a server-initiated debit from a hackathon build is not. So RETRY is\n")
    p("  recorded as intent and reports honestly that it did nothing. The gap is\n")
    p("  that the ENGINE does not know that, and pays a rung to find out.\n\n")
    p("  Two follow-ups fall out of this, and they are separate:\n")
    p("    - item 3a stops the engine choosing RETRY here at all.\n")
    p("    - the Ops tab attention list (2a) makes a FAILED action visible to a\n")
    p("      human, which is the gap that let this go unnoticed in the first place.\n")
    return out.getvalue()


body = run()
RULE = "=" * 76
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    "BEFORE-SHOT FOR ITEM 3a -- the engine spends a rung on an impossible RETRY\n"
    + RULE + "\n"
    f"captured {datetime.now():%Y-%m-%d %H:%M} local, at commit "
    f"{os.popen('git rev-parse --short HEAD').read().strip()}\n"
    "reproduce with: PYTHONPATH=. python scripts/capture_pre_3a.py\n\n"
    "G8_NON_RETRYABLE today only blocks retries the FAILURE CLASS rules out.\n"
    "It does not ask whether we hold an instrument to charge in the first place.\n"
    "Item 3a adds that: if not snap.is_mandate and snap.kind == ORDER, block RETRY\n"
    "with NO_MANDATE_TO_RETRY. The only lever on a one-time order is PAY_LINK.\n\n"
    + RULE + "\n\n" + body)
print(f"wrote {OUT.relative_to(ROOT)}\n")
print(body)
