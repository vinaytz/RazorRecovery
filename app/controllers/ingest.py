"""
Webhook ingest. Turns a Razorpay event into a case, or closes one.

Two rules, both learned expensively:

1. SUCCESS EVENTS MATTER AS MUCH AS FAILURES. A recovery engine that only
   subscribes to `payment.failed` chases people who already paid. Every
   settlement event closes its case as SELF_RECOVERED and cancels pending
   actions -- that is the difference between a tool and an embarrassment.
2. THE ENDPOINT DOES NO WORK. It verifies, inserts, returns 200. Razorpay
   retries on non-2xx, so slow logic in the handler becomes duplicate
   deliveries. `UNIQUE(events.dedupe_key)` absorbs the retries we do get.

Obligation identity: an obligation is the DEBT, not the payment. Many
`payment_id`s map to one obligation, so we key on order/invoice/subscription id
and fall back to the payment id only when there is nothing better.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.domain.models import ActionType, FailureClass

# Events that mean "the debt is gone". Anything here closes the case.
SETTLED_EVENTS = frozenset({
    "payment.captured", "order.paid", "subscription.charged", "invoice.paid",
})

# Events that mean "the debt exists and nobody has paid it".
FAILURE_EVENTS = frozenset({
    "payment.failed", "subscription.halted", "invoice.expired",
})

DOWNTIME_EVENTS = frozenset({
    "payment.downtime.started", "payment.downtime.resolved",
})

# Razorpay error_reason / error_code -> our FailureClass.
# This is the deterministic fallback. LLM job (1) in P8 handles the long tail of
# free-text `error_description` strings; this table handles the codes, and it is
# what runs when the LLM is dead.
ERROR_CODE_MAP: dict[str, FailureClass] = {
    "BAD_REQUEST_ERROR": FailureClass.UNKNOWN,
    "GATEWAY_ERROR": FailureClass.NETWORK_ERROR,
    "SERVER_ERROR": FailureClass.NETWORK_ERROR,
}

ERROR_REASON_MAP: dict[str, FailureClass] = {
    "insufficient_funds": FailureClass.INSUFFICIENT_FUNDS,
    "payment_failed_insufficient_balance": FailureClass.INSUFFICIENT_FUNDS,
    "card_expired": FailureClass.CARD_EXPIRED,
    "expired_card": FailureClass.CARD_EXPIRED,
    "card_blocked": FailureClass.CARD_BLOCKED,
    "card_disabled": FailureClass.CARD_BLOCKED,
    "payment_method_blocked": FailureClass.CARD_BLOCKED,
    "issuer_down": FailureClass.ISSUER_DOWN,
    "gateway_technical_error": FailureClass.ISSUER_DOWN,
    "bank_down": FailureClass.ISSUER_DOWN,
    "network_error": FailureClass.NETWORK_ERROR,
    "gateway_timeout": FailureClass.NETWORK_ERROR,
    "payment_timeout": FailureClass.AUTH_ABANDONED,
    "payment_cancelled_by_user": FailureClass.AUTH_ABANDONED,
    "otp_incorrect": FailureClass.AUTH_ABANDONED,
    "otp_attempts_exceeded": FailureClass.AUTH_ABANDONED,
    "upi_collect_expired": FailureClass.AUTH_ABANDONED,
    "invalid_mandate": FailureClass.MANDATE_INVALID,
    "mandate_revoked": FailureClass.MANDATE_INVALID,
    "mandate_not_found": FailureClass.MANDATE_INVALID,
    "checkout_abandoned": FailureClass.CHECKOUT_ABANDONED,
}


def classify(payload: dict, llm=None) -> FailureClass:
    """Razorpay error fields -> FailureClass. Never raises, never guesses wildly.

    Order matters and is deliberate: the deterministic `error_reason` map runs
    FIRST and wins. The LLM (job (1), SPEC 17) only ever sees the free-text
    `error_description` of a failure the map could not name. So the model cannot
    overrule a known code, and with `llm=None` this function behaves exactly as
    it did before P8 -- which is what keeps the benchmark reproducible.
    """
    pay = _payment_of(payload)
    reason = (pay.get("error_reason") or "").strip().lower()
    if reason in ERROR_REASON_MAP:
        return ERROR_REASON_MAP[reason]

    # The long tail: free text, issuer-specific wording, no matching code.
    if llm is not None:
        desc = (pay.get("error_description") or "").strip()
        if desc:
            fc = llm.classify_error(desc)
            if fc != FailureClass.UNKNOWN:
                return fc

    step = (pay.get("error_step") or "").strip().lower()
    src = (pay.get("error_source") or "").strip().lower()
    if step == "payment_authentication" or src == "customer":
        return FailureClass.AUTH_ABANDONED
    if src in ("bank", "issuer"):
        return FailureClass.ISSUER_DOWN

    code = (pay.get("error_code") or "").strip().upper()
    return ERROR_CODE_MAP.get(code, FailureClass.UNKNOWN)


def obligation_id_of(payload: dict) -> str:
    """The debt's identity. Order/invoice/subscription outranks payment id."""
    pay = _payment_of(payload)
    ent = _entities(payload)

    for key in ("subscription_id", "invoice_id", "order_id"):
        v = pay.get(key)
        if v:
            return str(v)
    for name in ("subscription", "invoice", "order"):
        v = (ent.get(name) or {}).get("entity", {}).get("id")
        if v:
            return str(v)
    v = pay.get("id")
    if v:
        return str(v)
    return "ob_unknown"


def dedupe_key_of(payload: dict, headers: dict | None = None) -> str:
    """Razorpay's own event id when present -- it is stable across retries."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    eid = headers.get("x-razorpay-event-id") or payload.get("id")
    if eid:
        return f"rzp:{eid}"
    # No event id: fall back to event type + entity + created_at. Two genuinely
    # distinct events cannot share all three.
    return (f"syn:{payload.get('event')}:{obligation_id_of(payload)}"
            f":{payload.get('created_at')}")


def amount_of(payload: dict) -> int:
    """Paise. int. Razorpay already sends paise, so no arithmetic and no float."""
    pay = _payment_of(payload)
    for key in ("amount", "amount_due"):
        v = pay.get(key)
        if isinstance(v, int):
            return v
    for name in ("order", "invoice", "subscription", "payment"):
        e = (_entities(payload).get(name) or {}).get("entity", {})
        for key in ("amount", "amount_due"):
            v = e.get(key)
            if isinstance(v, int):
                return v
    return 0


def method_of(payload: dict) -> str:
    m = (_payment_of(payload).get("method") or "").strip().lower()
    return m or "card"


def ingest(con, payload: dict, headers: dict | None = None,
           now: datetime | None = None, llm=None) -> dict:
    """Insert the event, then apply it. Idempotent by dedupe_key.

    Returns a verdict dict -- this is what the demo prints.
    """
    now = now or datetime.now()
    event = str(payload.get("event") or "unknown")
    oid = obligation_id_of(payload)
    key = dedupe_key_of(payload, headers)

    cur = con.execute(
        "INSERT OR IGNORE INTO events (dedupe_key, obligation_id, type, payload, received_at)"
        " VALUES (?,?,?,?,?)",
        (key, oid, event, json.dumps(payload), now.isoformat()))
    con.commit()

    if not cur.rowcount:
        # Already seen. Razorpay retries; this is the normal, healthy path.
        return {"event": event, "obligation_id": oid, "dedupe_key": key,
                "duplicate": True, "applied": False,
                "verdict": "duplicate ignored -- UNIQUE(events.dedupe_key)"}

    out: dict[str, Any] = {"event": event, "obligation_id": oid, "dedupe_key": key,
                           "duplicate": False, "applied": True}

    if event in SETTLED_EVENTS:
        out.update(_close_settled(con, oid, now))
    elif event in FAILURE_EVENTS:
        out.update(_open_case(con, payload, oid, now, llm))
    elif event in DOWNTIME_EVENTS:
        out.update({"action": "noted",
                    "verdict": "downtime recorded -- gate G9 blocks RETRY while it holds"})
    else:
        out.update({"action": "stored_only",
                    "verdict": f"'{event}' stored, no case logic for this type"})
    return out


def _open_case(con, payload: dict, oid: str, now: datetime, llm=None) -> dict:
    """A failure arrived. Create the obligation and the ENGINE case if new."""
    fc = classify(payload, llm)
    amount = amount_of(payload)
    pay = _payment_of(payload)
    cust = str(pay.get("customer_id") or pay.get("contact") or "cust_live")

    con.execute(
        "INSERT OR IGNORE INTO obligations (id, customer_id, amount_due, amount_settled,"
        " status, opened_at, contact, email, name) VALUES (?,?,?,?,?,?,?,?,?)",
        (oid, cust, amount, 0, "OPEN", now.isoformat(),
         pay.get("contact"), pay.get("email"), _name_of(payload)))
    # A later attempt on the same obligation is IGNOREd above, so backfill the
    # reachability we may not have had on the first webhook. COALESCE keeps what we
    # already know rather than overwriting a good address with a missing one.
    con.execute(
        "UPDATE obligations SET contact = COALESCE(contact, ?), email = COALESCE(email, ?),"
        " name = COALESCE(name, ?) WHERE id = ?",
        (pay.get("contact"), pay.get("email"), _name_of(payload), oid))

    case_id = f"live_{oid}"
    existing = con.execute("SELECT status, attempts FROM cases WHERE case_id = ?",
                           (case_id,)).fetchone()
    if existing:
        con.execute("UPDATE cases SET attempts = attempts + 1, failure_class = ? "
                    "WHERE case_id = ?", (fc.value, case_id))
        con.commit()
        return {"action": "case_updated", "case_id": case_id,
                "failure_class": fc.value, "amount": amount,
                "verdict": "another attempt on an obligation we already track"}

    con.execute(
        "INSERT INTO cases (case_id, run_id, obligation_id, customer_id, arm, amount,"
        " failure_class, method, kind, rung, attempts, status, contacts_sent,"
        " actions_taken, opened_at, closed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, "live", oid, cust, "ENGINE", amount, fc.value, method_of(payload),
         "ORDER", 0, 1, "OPEN", 0, 0, now.isoformat(), None))
    con.commit()
    return {"action": "case_opened", "case_id": case_id,
            "failure_class": fc.value, "amount": amount,
            "verdict": f"case opened, classified {fc.value}"}


def _close_settled(con, oid: str, now: datetime) -> dict:
    """The debt is gone. Close the case, cancel pending work, send nothing.

    This is the branch that stops us chasing a customer who already paid.
    """
    con.execute(
        "INSERT INTO obligations (id, customer_id, amount_due, amount_settled, status, opened_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
        " status='SETTLED', amount_settled=amount_due",
        (oid, "cust_live", 0, 0, "SETTLED", now.isoformat()))

    cur = con.execute(
        "UPDATE cases SET status='SELF_RECOVERED', closed_at=? "
        "WHERE obligation_id=? AND status='OPEN'", (now.isoformat(), oid))
    closed = cur.rowcount

    cancelled = con.execute(
        "UPDATE actions SET status='CANCELLED', detail='obligation settled' "
        "WHERE obligation_id=? AND status='PENDING'", (oid,)).rowcount
    con.commit()

    return {"action": "case_closed", "cases_closed": closed,
            "actions_cancelled": cancelled,
            "verdict": (f"settled -- {closed} case(s) closed SELF_RECOVERED, "
                        f"{cancelled} pending action(s) cancelled. nothing sent.")}


# -- payload shape helpers -------------------------------------------------
# Razorpay nests entities under payload.<name>.entity. Every accessor tolerates
# a missing layer, because a webhook shape you did not expect must not 500 --
# a 500 makes Razorpay retry, and now you have a retry storm on top of a bug.

def _entities(payload: dict) -> dict:
    p = payload.get("payload")
    return p if isinstance(p, dict) else {}


def _payment_of(payload: dict) -> dict:
    e = (_entities(payload).get("payment") or {}).get("entity")
    if isinstance(e, dict):
        return e
    for name in ("subscription", "invoice", "order"):
        e = (_entities(payload).get(name) or {}).get("entity")
        if isinstance(e, dict):
            return e
    return {}


def _name_of(payload: dict) -> str | None:
    """The customer's name, if the payload happens to carry one.

    Razorpay does not put a name on every entity, so this is best-effort and the
    notifier falls back to a neutral greeting. Guessing a name is worse than not
    using one.
    """
    pay = _payment_of(payload)
    for src in (pay.get("customer_details"), pay.get("customer"), pay.get("notes")):
        if isinstance(src, dict) and src.get("name"):
            return str(src["name"])[:120]
    v = pay.get("customer_name") or pay.get("name")
    return str(v)[:120] if v else None
