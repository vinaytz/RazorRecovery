"""Capture what the engine does with a one-time order that holds no mandate.

Originally the before-shot for item 3a: the engine spending rung 1 on a RETRY that
cannot possibly reach money. It now reports whichever world it is run in.

  before 3a  ->  G8 passes, the engine picks RETRY, the executor answers
                 INTENT_ONLY, and the rung is gone.  docs/evidence/pre_3a_wasted_rung.txt
  after 3a   ->  G8 blocks RETRY with NO_MANDATE_TO_RETRY, the ladder climbs past
                 the dead rung to a real lever.       docs/evidence/post_3a_retry_blocked.txt

It observes rather than asserts, and writes to the filename matching what it saw,
so neither evidence file can quietly become a description of the other world. The
before-shot records the commit it was taken at; reproducing it means checking that
commit out, because on this one the gate is there and cannot be talked out of it.
"""
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["DRY_RUN"] = "false"          # dry run short-circuits before the RETRY branch
os.environ["TIME_SCALE"] = "7200"

from app.controllers import ingest as ingest_ctl          # noqa: E402
from app.domain.models import ActionType                  # noqa: E402
from app.repos import store                               # noqa: E402
from app.services.executor import build_executor          # noqa: E402
from app.services.llm import get_llm                      # noqa: E402
from app.workers.live import LiveWorker                   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"
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


def run():
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

    # Run until the case does something other than wait, then report on it.
    first_real = None
    for _ in range(200):
        tick = w.tick(clock.now())
        for d in tick["decided"]:
            if d.get("action") not in (None, "WAIT", "NONE") and first_real is None:
                first_real = d["action"]
        if first_real is not None and tick["executed"]:
            break
        time.sleep(0.2)

    row = con.execute("SELECT gate_trace FROM decisions WHERE case_id=?"
                      " AND action NOT IN ('WAIT','NONE') ORDER BY rowid LIMIT 1",
                      (cid,)).fetchone()
    trace = json.loads(row[0]) if row else []
    g8 = next((g for g in trace if g["gate"] == "G8_NON_RETRYABLE"), None)
    retry_blocked = bool(g8) and not g8["passed"] and "NO_MANDATE" in (g8["detail"] or "")

    p("WHAT THE GATES SAY\n")
    for g in trace:
        mark = "pass " if g["passed"] else "BLOCK"
        star = ""
        if g["gate"] == "G8_NON_RETRYABLE":
            star = ("   <-- blocks RETRY. no instrument, no debit." if retry_blocked
                    else "   <-- passes. nothing here knows RETRY is impossible.")
        p(f"  {g['gate']:<20}{mark} {g['detail']}{star}\n")
    p(f"\n  -> the engine chooses {first_real}.\n\n")

    p("WHAT ACTUALLY HAPPENS WHEN IT RUNS\n")
    for r in con.execute("SELECT type, status, detail FROM actions WHERE case_id=?"
                         " ORDER BY rowid", (cid,)):
        p(f"  {r[0]:<12}{r[1]:<10}{r[2] or ''}\n")
    rung, attempts, contacts = con.execute(
        "SELECT rung, attempts, contacts_sent FROM cases WHERE case_id=?",
        (cid,)).fetchone()
    p(f"\n  rung {rung}   attempts {attempts}   contacts_sent {contacts}\n\n")

    if retry_blocked:
        p("  The dead rung was never spent. The ladder skipped it -- which is not\n")
        p("  jumping the ladder, because a hard gate made the rung illegal rather\n")
        p("  than merely unattractive -- and the first thing this customer gets is\n")
        p("  a lever that can actually move money.\n\n")
        p("WHAT THE GATE COST AND WHAT IT BOUGHT\n")
        p("  It is not free. `sim/world.py` gives RETRY a real success probability\n")
        p("  on these cases (0.60 on ISSUER_DOWN, 0.45 on NETWORK_ERROR), so\n")
        p("  blocking it costs the benchmark money: incremental fell from\n")
        p("  Rs 10,02,742 to Rs 9,66,003. The effect matrix is an input and was not\n")
        p("  touched to make this look better.\n\n")
        p("  What it bought is that the number is now about actions that exist. The\n")
        p("  fixed-retry BASELINE fell much further (Rs 15,39,397 -> Rs 13,08,719),\n")
        p("  because a naive retry policy is exactly what this gate takes away, and\n")
        p("  the engine's share of the oracle ceiling rose from 64.8% to 76.2%.\n")
    else:
        p("  The rung was spent. The customer heard nothing. The only thing that\n")
        p("  moved is the ladder position -- which now sits one rung closer to\n")
        p("  WRITE_OFF than it did before, having bought nothing.\n\n")
        p("WHERE THAT STRING SURFACES\n")
        p("  There is no log line. `app/controllers/execute.py` does not log at all,\n")
        p("  so a rung burned on an impossible RETRY is silent to an operator today.\n")
        p("  The string exists in exactly two places:\n\n")
        r = con.execute("SELECT action_id, type, status, detail FROM actions"
                        " WHERE case_id=? AND type='RETRY'", (cid,)).fetchone()
        if r:
            p(f"  1. actions.detail       {r[0]}  {r[1]}  {r[2]}\n")
            p(f"                          {r[3]!r}\n")
        p("  2. the JSON returned by POST /api/worker/tick, under executed[].detail\n\n")
        p("  executor.py:481 -- RazorpayExecutor never calls a charge API. See its\n")
        p("  class docstring: creating a payment link is reversible and rate-limited;\n")
        p("  a server-initiated debit from a hackathon build is not. So RETRY is\n")
        p("  recorded as intent and reports honestly that it did nothing. The gap is\n")
        p("  that the ENGINE does not know that, and pays a rung to find out.\n")

    return out.getvalue(), retry_blocked


body, blocked = run()
RULE = "=" * 76
head = (
    ("AFTER-SHOT FOR ITEM 3a -- G8 refuses a retry there is no instrument for\n"
     if blocked else
     "BEFORE-SHOT FOR ITEM 3a -- the engine spends a rung on an impossible RETRY\n")
    + RULE + "\n"
    f"captured {datetime.now():%Y-%m-%d %H:%M} local, at commit "
    f"{os.popen('git rev-parse --short HEAD').read().strip()}\n"
    "reproduce with: PYTHONPATH=. python scripts/capture_pre_3a.py\n\n")
head += (
    ("G8 now asks two questions, not one: is the instrument usable, and is there\n"
     "an instrument at all. `not snap.is_mandate` blocks RETRY with\n"
     "NO_MANDATE_TO_RETRY. The test is the mandate rather than the obligation kind,\n"
     "because a checkout abandonment never held an instrument either and a gate\n"
     "keyed on kind == ORDER would have missed it.\n\n") if blocked else
    ("G8_NON_RETRYABLE today only blocks retries the FAILURE CLASS rules out.\n"
     "It does not ask whether we hold an instrument to charge in the first place.\n"
     "Item 3a adds that: if not snap.is_mandate, block RETRY with\n"
     "NO_MANDATE_TO_RETRY. The only lever on a one-time order is PAY_LINK.\n\n"))

OUT = EVIDENCE / ("post_3a_retry_blocked.txt" if blocked else "pre_3a_wasted_rung.txt")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(head + RULE + "\n\n" + body)
print(f"wrote {OUT.relative_to(ROOT)}\n")
print(body)
