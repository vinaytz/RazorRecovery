"""
The abandoned-checkout sweeper. The second source of at-risk revenue.

Everything else in this system is driven by an event. A payment fails, Razorpay
tells us, we open a case. Checkout abandonment has no such event -- there is no
`checkout.abandoned` webhook, because closing a tab is not something a payment
gateway can observe. The signal is an ABSENCE:

    the order exists, and `payment.captured` never arrived.

An absence cannot be pushed to us, so it has to be swept for. That is the only
architectural difference between this source and the failed-payment source, and
it is confined to this file. Once the sweeper decides a checkout was abandoned it
writes an obligation and a case exactly like `ingest._open_case` does, and from
that moment on the case is indistinguishable to everything downstream:

    same CaseSnapshot     -- CHECKOUT_ABANDONED was already in FailureClass
    same gates            -- G0 holdout first, then the rest, unchanged
    same ladder           -- and RETRY is correctly useless here, see below
    same engine           -- decide() cannot tell how the case was born
    same executor         -- one PAY_LINK path for every contact action

ZERO changes to app/domain/. That is the test of whether the abstraction was
right: a genuinely new revenue source should need new plumbing, not a new core.
`tests/test_abandonment.py` asserts it, and `git diff --stat` shows it.

WHERE THE FIRST HALF OF THE SIGNAL COMES FROM, and why it is not a webhook.
Knowing the order exists is the input to this whole file, and **Razorpay does not
broadcast order creation** -- there is no `order.created` in their webhook event
list, because creating an order is a server-side call the merchant makes and a
gateway cannot announce something it did not observe. So this source has one
dependency on merchant code that the failed-payment source does not:
`POST /api/orders/watch`, called right after `orders.create()`. It is one line in
their backend, it is documented in README, and it is the ONLY production feed.
An earlier draft of this module read an `order.created` webhook, which meant the
sweeper would have swept an empty table forever in production while every unit
test passed on a hand-built payload. `tests/test_order_watch.py` pins the fix.

WHY THE DELAY IS A CONFIG AND NOT A CONSTANT. `ABANDON_MINUTES` (default 30) is
the shortest wait we are willing to call a decision. Too short and we email a
customer who is still typing their OTP -- the most expensive false chase there
is, because it arrives while they are actively paying. Too long and the intent is
gone. 30 minutes is well past every UPI collect expiry and 3DS timeout.

WHAT THE ENGINE WILL DO WITH THESE, and why that is right. A checkout
abandonment is a one-time ORDER with no mandate, so there is no instrument to
charge and RETRY is meaningless -- the only real lever is PAY_LINK. Item 3a is
the gate that states this: G8 now blocks RETRY whenever `is_mandate` is false,
which covers these. Note it is keyed on the mandate and not on the obligation
kind -- these cases are opened as kind=CHECKOUT, so a gate written against
`kind == ORDER` would have missed exactly the case this paragraph is about.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from app.domain.models import FailureClass, ObligationKind
from app.repos import store

log = logging.getLogger("razorrecovery.sweeper")

DEFAULT_ABANDON_MINUTES = 30


def abandon_minutes() -> int:
    """`ABANDON_MINUTES`. Read per call so the demo can shorten the window live."""
    raw = os.environ.get("ABANDON_MINUTES", "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ABANDON_MINUTES
    return v if v > 0 else DEFAULT_ABANDON_MINUTES


def sweep(con, now: datetime | None = None, minutes: int | None = None,
          limit: int = 200) -> dict:
    """Turn every checkout whose window has closed into an open case.

    Idempotent by construction: `resolve_checkout` only moves a WATCHING row, so a
    second sweep in the same second finds nothing to do. The case id is
    `live_{order_id}`, the same id `ingest` would mint, so a `payment.failed` that
    arrives later updates the case rather than creating a rival one.
    """
    now = now or datetime.now()
    window = minutes if minutes is not None else abandon_minutes()
    cutoff = (now - timedelta(minutes=window)).isoformat()

    opened, skipped = [], []
    for row in store.due_checkouts(con, cutoff, limit):
        oid = row["order_id"]

        # A settlement may have landed without us seeing the success webhook --
        # a level-4 amount match, or an operator's ledger entry. Chasing a debt
        # the ledger already calls settled is the one unforgivable false chase.
        settled = con.execute(
            "SELECT status FROM obligations WHERE id = ? AND status = 'SETTLED'",
            (oid,)).fetchone()
        if settled:
            store.resolve_checkout(con, oid, "PAID", now.isoformat(),
                                   "settled before the sweep ran")
            skipped.append({"order_id": oid, "why": "already settled"})
            continue

        case_id = _open_abandoned_case(con, row, now)
        store.resolve_checkout(
            con, oid, "ABANDONED", now.isoformat(),
            f"no payment within {window} minutes of order creation")
        opened.append({"order_id": oid, "case_id": case_id, "amount": row["amount"]})

    if opened:
        log.info("sweeper opened %d abandoned checkout(s) worth %d paise",
                 len(opened), sum(o["amount"] for o in opened))

    return {
        "swept_at": now.isoformat(), "window_minutes": window,
        "opened": opened, "skipped": skipped,
        "verdict": (f"{len(opened)} checkout(s) abandoned for more than {window} "
                    f"minutes became cases; {len(skipped)} had already been paid"),
    }


def _open_abandoned_case(con, row: dict, now: datetime) -> str:
    """Write the obligation and the case. Deliberately the same shape as ingest.

    `kind` is CHECKOUT, which changes exactly one thing in the engine: it is not
    in ESSENTIAL_KINDS, so the ordinary (lower) AFA limit applies. It is not a
    subscription and pretending otherwise would buy a looser re-auth ceiling for a
    debt nobody has authorised at all.
    """
    oid = row["order_id"]
    amount = int(row["amount"] or 0)
    cust = row["customer_id"] or f"cust_{oid}"

    con.execute(
        "INSERT OR IGNORE INTO obligations (id, customer_id, amount_due, amount_settled,"
        " status, opened_at, contact, email, name) VALUES (?,?,?,?,?,?,?,?,?)",
        (oid, cust, amount, 0, "OPEN", row["created_at"],
         row["contact"], row["email"], row["name"]))
    con.execute(
        "UPDATE obligations SET contact = COALESCE(contact, ?), email = COALESCE(email, ?),"
        " name = COALESCE(name, ?) WHERE id = ?",
        (row["contact"], row["email"], row["name"], oid))

    case_id = f"live_{oid}"
    existing = con.execute("SELECT case_id FROM cases WHERE case_id = ?",
                           (case_id,)).fetchone()
    if not existing:
        con.execute(
            "INSERT INTO cases (case_id, run_id, obligation_id, customer_id, arm, amount,"
            " failure_class, method, kind, rung, attempts, status, contacts_sent,"
            " actions_taken, opened_at, closed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, "live", oid, cust, "ENGINE", amount,
             FailureClass.CHECKOUT_ABANDONED.value, row["method"] or "unknown",
             ObligationKind.CHECKOUT.value,
             # rung 0, attempts 0. Nobody tried to pay and failed -- they never
             # completed an attempt at all. Calling this attempt 1 would make the
             # bandit's `attempts` feature mean two different things.
             0, 0, "OPEN", 0, 0, row["created_at"], None))
    con.commit()
    return case_id
