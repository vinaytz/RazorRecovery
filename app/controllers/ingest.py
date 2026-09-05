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

from app.domain.models import FailureClass
from app.repos import store
from app.services import matcher
from app.workers import sweeper

# Events that mean "the debt is gone". Anything here closes the case.
# `payment_link.paid` is here because a link we minted can settle without us ever
# seeing the merchant's order id -- see app/services/matcher.py on why.
SETTLED_EVENTS = frozenset({
    "payment.captured", "order.paid", "subscription.charged", "invoice.paid",
    "payment_link.paid",
})

# Events that mean "the debt exists and nobody has paid it".
FAILURE_EVENTS = frozenset({
    "payment.failed", "subscription.halted", "invoice.expired",
})

# `order.created` is not a failure and not a settlement -- it is the START of a
# window in which a failure can happen by NOTHING happening. It opens no case. It
# puts the order on a watch list, and `app/workers/sweeper.py` decides later
# whether the silence meant abandonment. See that module's docstring.
WATCH_EVENTS = frozenset({"order.created"})

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
        out.update(_close_settled(con, payload, oid, now))
    elif event in FAILURE_EVENTS:
        out.update(_open_case(con, payload, oid, now, llm))
    elif event in WATCH_EVENTS:
        out.update(_watch_checkout(con, payload, oid, now))
    elif event in DOWNTIME_EVENTS:
        out.update(_record_downtime(con, payload, event, now))
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


def _watch_checkout(con, payload: dict, oid: str, now: datetime) -> dict:
    """An order exists and nobody has paid it yet. Start the clock, open nothing.

    This is the quietest handler in the file and it does the most damage if it
    gets loud. An order that is 200 milliseconds old is not at risk -- the
    customer is looking at the payment page. Opening a case here would put every
    successful checkout the merchant has ever had into the recovery funnel.
    """
    order = (_entities(payload).get("order") or {}).get("entity", {})
    pay = _payment_of(payload)
    amount = amount_of(payload)
    cust = str(order.get("customer_id") or pay.get("customer_id")
               or pay.get("contact") or f"cust_{oid}")

    # An order entity carries no `contact`/`email` of its own -- there is no payment
    # yet, so there is no payer on it. What reachability exists is in `notes`, put
    # there by the merchant's own checkout integration. Without this the sweeper
    # opens a case for a customer it has no way to email, which G13 would then
    # spend a contact slot discovering.
    notes = pay.get("notes") if isinstance(pay.get("notes"), dict) else {}
    fresh = store.watch_checkout(
        con, order_id=oid, customer_id=cust, amount=amount,
        method=(order.get("method") or pay.get("method") or "unknown"),
        contact=pay.get("contact") or notes.get("contact"),
        email=pay.get("email") or notes.get("email"),
        name=_name_of(payload), receipt=order.get("receipt"),
        created_at=_order_created_at(order, payload, now), seen_at=now.isoformat())

    window = sweeper.abandon_minutes()
    return {"action": "checkout_watched" if fresh else "checkout_already_watched",
            "amount": amount, "abandon_minutes": window,
            "verdict": (f"order on the watch list -- NO case opened. if no payment "
                        f"arrives within {window} minutes the sweeper opens one")
            if fresh else "already watching this order -- the clock is not reset"}


def _order_created_at(order: dict, payload: dict, now: datetime) -> str:
    """When the ORDER was created, not when we heard about it.

    The abandonment window measures the customer's silence, so it has to start when
    they were last seen -- not when the webhook reached us. Webhooks are retried,
    queued behind an outage, and replayed by hand; starting the clock at receipt
    would hand a 40-minute-late delivery a fresh 30 minutes of grace, and the older
    the delivery the longer the customer waits to hear from us. Razorpay sends
    `created_at` as Unix epoch SECONDS.

    Falls back to `now` when the payload has no usable timestamp, which is the
    conservative direction: we wait longer rather than chase sooner.
    """
    for src in (order, payload):
        ts = src.get("created_at")
        if isinstance(ts, int) and ts > 0:
            try:
                return datetime.fromtimestamp(ts).isoformat()
            except (OverflowError, OSError, ValueError):
                continue
    return now.isoformat()


def _record_downtime(con, payload: dict, event: str, now: datetime) -> dict:
    """Razorpay says an issuer is down, or back. Store it. Predict nothing.

    `app/workers/live.py::snapshot` is what carries a row here into the engine.
    Before this handler existed the branch replied "downtime recorded -- gate G9
    blocks RETRY while it holds" and recorded nothing at all, so the verdict was
    false and every live snapshot said `method_in_downtime=False` through an outage.

    WHAT AN OUTAGE COSTS IS THE WHOLE CASE, NOT JUST ITS RETRIES. G9 does two
    things: it adds RETRY to `blocked_actions`, and it sets `wait_until`.
    `app/domain/engine.py` returns WAIT for any gate that set a `wait_until` in the
    future, so during a downtime the case does not fall through to REMIND or
    PAY_LINK -- it waits. That is deliberate and predates this handler: the customer
    cannot pay on a dead rail, so a "pay now" message points at one and burns a
    contact slot to say nothing. It is written down because the first draft of this
    item claimed the opposite. `downtime_backoff_minutes` is how long until we look
    again, and `tests/test_downtime.py` pins it against the engine rather than
    against the ladder, which never sees the wait.

    THE END TIME IS COPIED, NEVER COMPUTED. `payment.downtime.started` carries
    `end: null`, because nobody knows when an outage lifts. When that is what
    arrives, `downtime_ends_at` stays None and G9 falls back to
    `downtime_backoff_minutes` -- a re-check interval, not a forecast. A scheduled
    maintenance window is the one case that arrives carrying a real `end`, and then
    we use Razorpay's number. There is no branch here that estimates one.

    WHAT THIS BLOCKS IS BROADER THAN THE OUTAGE. Razorpay scopes downtime to an
    instrument -- HDFC netbanking, not netbanking -- and `CaseSnapshot` has a
    `method` and no instrument. So an HDFC outage holds every netbanking case,
    including ICICI's. That is over-blocking, it is stated rather than hidden, and
    it errs toward not burning retries. The sharper cost is PAY_LINK, which is
    method-agnostic in practice and gets deferred anyway. Narrowing either would
    mean a new domain field and a matching change to `sim/runner.py`, which would
    move the benchmark md5; the instrument is recorded here so a later item can do
    that without re-ingesting anything.
    """
    d = (_entities(payload).get("payment.downtime") or {}).get("entity")
    d = d if isinstance(d, dict) else {}
    method = (d.get("method") or "").strip().lower() or "unknown"
    did = str(d.get("id") or payload.get("id") or f"down_{method}_{now.isoformat()}")
    resolved = event.endswith(".resolved")
    ends_at = _epoch_iso(d.get("end"))

    if resolved:
        n = store.resolve_downtime(con, downtime_id=did, method=method,
                                   when=now.isoformat(), ends_at=ends_at)
        return {"action": "downtime_resolved" if n else "downtime_resolve_ignored",
                "method": method, "downtime_id": did,
                "verdict": (f"{method} downtime cleared -- G9 stops holding those cases"
                            if n else
                            f"no open {method} downtime to clear -- nothing changed")}

    outcome = store.record_downtime(
        con, downtime_id=did, method=method,
        instrument=json.dumps(d["instrument"]) if isinstance(d.get("instrument"), dict) else None,
        severity=(d.get("severity") or None), scheduled=bool(d.get("scheduled")),
        began_at=_epoch_iso(d.get("begin")) or now.isoformat(), ends_at=ends_at,
        seen_at=now.isoformat())

    # The verdict describes what is true after the write, not what the event asked
    # for. A `.started` for an outage we have already seen resolved changes nothing
    # and must not claim a block -- that is the exact defect this item was opened to
    # fix, and it hides one layer down if the reply is written from the payload.
    if outcome == "already_resolved":
        verdict = (f"this {method} outage is already marked resolved -- not reopening "
                   f"it. webhook order is not guaranteed, so a late .started must "
                   f"never un-resolve one. nothing is being held on {method}")
    else:
        held = ("still held" if outcome == "already_open" else "now held")
        verdict = (f"{method} is down -- G9 blocks RETRY and every open {method} case "
                   f"is {held} "
                   + ("until " + ends_at if ends_at else
                      "until Razorpay sends .resolved. no end time was sent and "
                      "none is guessed"))

    return {"action": f"downtime_{outcome}", "method": method, "downtime_id": did,
            "severity": d.get("severity"), "ends_at": ends_at,
            "blocking": outcome != "already_resolved", "verdict": verdict}


def _epoch_iso(ts) -> str | None:
    """Razorpay's Unix seconds -> iso. None stays None, and that is the point.

    `end` is null on a live outage. Returning None here is what keeps
    `downtime_ends_at` empty instead of inventing a plausible-looking timestamp.
    """
    if not isinstance(ts, int) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _close_settled(con, payload: dict, oid: str, now: datetime) -> dict:
    """The debt is gone. Work out WHICH debt, close it, cancel pending work.

    This is the branch that stops us chasing a customer who already paid, and the
    branch that decides whether the money counts as ours. Both halves are
    deliberately separate:

      MATCHING  which obligation did this payment settle, and how sure are we
                (`app/services/matcher.py`, levels 1-5)
      ATTRIBUTION  did we do anything before it paid

    Attribution is NOT causation. A case marked RECOVERED means we contacted the
    customer and then they paid; it does not prove they paid because of us. The
    only number that carries that claim is the holdout comparison in the
    benchmark, and it is measured against customers we deliberately never touch.
    """
    m = matcher.match_settlement(con, payload, now)
    amount = matcher.amount_of_settlement(payload)
    payment_id = matcher.payment_id_of(payload)
    method = method_of(payload)

    # Take it off the abandonment watch list FIRST, before any matching verdict.
    # This runs even when the settlement is unmatched, because the ids on a
    # payment are enough to prove THIS order was paid whether or not we can tell
    # which debt it closed -- and a paid order left on the watch list becomes an
    # abandoned-checkout case 30 minutes later. Every id in the payload is
    # cleared: a link payment carries Razorpay's own order id alongside ours.
    for cid in matcher.candidate_ids(payload):
        store.resolve_checkout(con, cid, "PAID", now.isoformat(),
                               f"paid by {payment_id}")

    if not m.matched:
        # Money we saw arrive and cannot attribute. Recorded, not guessed at, and
        # surfaced for a human -- silently dropping it would make the ledger drift.
        fresh = store.record_settlement(
            con, payment_id=payment_id, obligation_id=None, case_id=None,
            amount=amount, method=method, match_level=m.level, match_basis=m.basis,
            match_confidence=m.confidence, match_evidence=m.evidence,
            candidates=m.candidates, attributed=False,
            attribution_reason="nothing closed -- this settlement is unmatched",
            settled_at=now.isoformat(), source="webhook")
        return {"action": "settlement_unmatched", "match": m.as_dict(),
                "counted": fresh,
                "verdict": (f"settlement of {amount} paise could not be matched to an "
                            f"open debt ({m.evidence}) -- recorded for review, "
                            f"NOTHING closed")}

    target = m.obligation_id
    con.execute(
        "INSERT INTO obligations (id, customer_id, amount_due, amount_settled, status,"
        " opened_at) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
        " status='SETTLED', amount_settled=MAX(amount_settled, ?)",
        (target, "cust_live", amount, amount, "SETTLED", now.isoformat(), amount))

    # ATTRIBUTION. Two things can make a settlement ours, and both are facts we
    # recorded ourselves rather than inferences about the customer.
    paid_our_link = matcher.via_our_link(payload)
    contact = store.was_contacted(con, target)
    if paid_our_link:
        attributed, why = True, "paid through the payment link we sent"
    elif contact:
        attributed, why = True, (f"we sent a {contact['action']} on "
                                 f"{contact['sent_at']} before this settled")
    else:
        attributed, why = False, "no contact had been made when this settled"

    status = "RECOVERED" if attributed else "SELF_RECOVERED"
    cur = con.execute(
        "UPDATE cases SET status=?, closed_at=?, match_level=?, match_basis=?,"
        " match_confidence=? WHERE obligation_id=? AND status='OPEN'",
        (status, now.isoformat(), m.level, m.basis, m.confidence, target))
    closed = cur.rowcount
    case_row = con.execute("SELECT case_id FROM cases WHERE obligation_id=? LIMIT 1",
                           (target,)).fetchone()

    cancelled = con.execute(
        "UPDATE actions SET status='CANCELLED', detail='obligation settled' "
        "WHERE obligation_id=? AND status='PENDING'", (target,)).rowcount
    links = store.mark_payment_link(con, target, "paid")
    con.commit()

    fresh = store.record_settlement(
        con, payment_id=payment_id, obligation_id=target,
        case_id=case_row["case_id"] if case_row else None,
        amount=amount, method=method, match_level=m.level, match_basis=m.basis,
        match_confidence=m.confidence, match_evidence=m.evidence,
        candidates=m.candidates, attributed=attributed, attribution_reason=why,
        settled_at=now.isoformat(), source="webhook")

    return {"action": "case_closed", "obligation_id": target,
            "cases_closed": closed, "actions_cancelled": cancelled,
            "links_closed": links, "status": status,
            "match": m.as_dict(), "attributed": attributed,
            "attribution_reason": why, "counted": fresh,
            "verdict": (f"settled -- matched at level {m.level} "
                        f"({m.basis}, {m.confidence}): {m.evidence}. "
                        f"{closed} case(s) closed {status}, {cancelled} pending "
                        f"action(s) cancelled, {links} link(s) closed. nothing sent."
                        + ("" if fresh else " already counted, not double-counted."))}


def settle_from_ledger(con, case_id: str, now: datetime, *, amount: int | None = None,
                       who: str | None = None, reference: str | None = None,
                       note: str | None = None) -> dict:
    """Level 5. Cash, bank transfer, a cheque -- money with no webhook.

    Deliberately not 'certain': we are recording somebody's word. It closes the
    case exactly like a webhook would, so the customer stops being chased, but the
    match level on the row says where the fact came from.
    """
    row = con.execute("SELECT case_id, obligation_id, amount, customer_id FROM cases"
                      " WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no case '{case_id}'"}

    target = row["obligation_id"]
    paid = int(amount if amount is not None else (row["amount"] or 0))
    m = matcher.ledger_match(case_id, target, who, reference)

    con.execute("UPDATE obligations SET status='SETTLED',"
                " amount_settled=MAX(amount_settled, ?) WHERE id = ?", (paid, target))
    closed = con.execute(
        "UPDATE cases SET status='RECOVERED', closed_at=?, match_level=?,"
        " match_basis=?, match_confidence=? WHERE case_id=? AND status='OPEN'",
        (now.isoformat(), m.level, m.basis, m.confidence, case_id)).rowcount
    cancelled = con.execute(
        "UPDATE actions SET status='CANCELLED', detail='settled out of band' "
        "WHERE obligation_id=? AND status='PENDING'", (target,)).rowcount
    links = store.mark_payment_link(con, target, "cancelled")
    con.commit()

    contact = store.was_contacted(con, target)
    why = ((f"we sent a {contact['action']} on {contact['sent_at']} before this "
            f"was recorded") if contact else
           "recorded manually with no contact on file")
    fresh = store.record_settlement(
        con, payment_id=f"ledger:{case_id}", obligation_id=target, case_id=case_id,
        amount=paid, method="offline", match_level=m.level, match_basis=m.basis,
        match_confidence=m.confidence,
        match_evidence=m.evidence + (f" -- {note}" if note else ""),
        candidates=1, attributed=bool(contact), attribution_reason=why,
        settled_at=now.isoformat(), source="ledger_hook")

    return {"ok": True, "case_id": case_id, "obligation_id": target,
            "cases_closed": closed, "actions_cancelled": cancelled,
            "links_closed": links, "match": m.as_dict(), "counted": fresh,
            "verdict": (f"recorded at level 5 (asserted, not observed): {m.evidence}. "
                        f"{closed} case(s) closed, {cancelled} action(s) cancelled."
                        + ("" if fresh else " already recorded."))}



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
