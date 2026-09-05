"""
The merchant-side checkout page. Where a "customer" pays, in the demo.

WHY THIS EXISTS. Every other surface in this project starts from an event that
already happened. There was no page where a person types their email, picks an
amount, and pays -- so there was no way to show the thing the whole system is
about: a real customer, a real Razorpay screen, a real outcome, landing in the
recovery dashboard while you watch.

This is that page. `GET /checkout` serves the form; the endpoints below are the
merchant backend behind it.

WHAT IS REAL AND WHAT IS NOT -- read this before demoing.

  the order            REAL when RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are set.
                       `client.order.create()`, test mode, a genuine order_id.
  the payment screen   REAL. Razorpay Checkout, the merchant's own iframe, with
                       whatever methods the test account has enabled.
  the money            test mode, so no money moves. That is Razorpay's doing,
                       not ours.
  the outcome          REAL, and FETCHED BACK FROM RAZORPAY. `/api/checkout/paid`
                       and `/api/checkout/failed` do not trust what the browser
                       tells them. They call `client.payment.fetch(payment_id)`
                       and build the event from the entity Razorpay returns.
  the transport        NOT a webhook, on localhost. See the next paragraph.

THE TRANSPORT IS THE ONE COMPROMISE, AND IT IS DELIBERATE. In production Razorpay
POSTs `payment.captured` to `/webhooks/razorpay`. It cannot reach a laptop without
a tunnel, and tunnels die on demo day. So the browser tells us the payment id, we
fetch the payment from Razorpay's API to find out what actually happened, and then
we hand the resulting entity to the SAME `ingest.ingest` the webhook handler calls.
The payload is Razorpay's, the code path is production's; only who carried the
message differs. Set up a tunnel and the real webhook works too -- the duplicate
collapses on `UNIQUE(settlements.payment_id)`, so the money cannot be counted
twice either way.

Without credentials nothing above is available, so the page degrades to
`/api/checkout/simulate-failure`, which is honestly named: it builds the payload
itself. It exists so the demo survives a dead network, and it says `simulated:
true` in every response.

THE ORDER OF OPERATIONS IN `/api/checkout` MATTERS. The order is put on the
abandonment watch list BEFORE the browser is told to open Checkout. If the
customer closes the modal there is no event at all -- abandonment is an absence --
and the only thing that can catch it is a clock that was already running.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from app.api import dashboard
from app.controllers import ingest as ingest_ctl
from app.workers import sweeper

log = logging.getLogger("razorrecovery.checkout")

router = APIRouter(tags=["checkout"])
ROOT = Path(__file__).resolve().parents[2]

# The demo store's name, on the page and on the Razorpay modal.
MERCHANT_NAME = os.getenv("MERCHANT_NAME") or "Acme Retail"


def con():
    """The dashboard's connection, deliberately. One database, one live run --
    a checkout that wrote somewhere else would not appear on the dashboard."""
    return dashboard.con()


def _client():
    """A Razorpay client if credentials are present, else None. Never raises.

    Same degradation as `executor.build_executor`: four things can be missing and
    none of them may take the page down.
    """
    key, secret = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key or not secret:
        return None
    try:
        import razorpay                              # noqa: PLC0415
        return razorpay.Client(auth=(key, secret))
    except Exception as e:                           # noqa: BLE001
        log.warning("razorpay SDK unavailable (%s: %s) -- checkout runs simulated",
                    type(e).__name__, e)
        return None


def _event(name: str, entity: dict, event_id: str) -> dict:
    """Wrap a payment entity in the webhook envelope Razorpay would have sent.

    The envelope is ours; `entity` came from Razorpay's API. `ingest` reads the
    entity and dedupes on the envelope's id, so the id is derived from the payment
    id rather than a timestamp -- a double-clicked handler collapses.
    """
    return {"entity": "event", "account_id": os.getenv("RAZORPAY_ACCOUNT_ID", "acc_local"),
            "event": name, "contains": ["payment"], "id": event_id,
            "created_at": int(time.time()),
            "payload": {"payment": {"entity": entity}}}


def _fetch_payment(client, payment_id: str) -> dict:
    if client is None:
        raise HTTPException(400, {
            "error": "no Razorpay credentials -- cannot verify this payment",
            "how": "set RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET, or use "
                   "POST /api/checkout/simulate-failure for a credential-less demo"})
    try:
        entity = client.payment.fetch(payment_id)
    except Exception as e:                           # noqa: BLE001
        raise HTTPException(502, f"payment.fetch({payment_id}) failed: "
                                 f"{type(e).__name__}: {e}")
    if not isinstance(entity, dict) or not entity.get("id"):
        raise HTTPException(502, f"payment.fetch({payment_id}) returned no entity")
    return entity


def _row(order_id: str):
    row = con().execute("SELECT * FROM checkouts WHERE order_id = ?",
                        (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"no order '{order_id}' -- POST /api/checkout first")
    return row


@router.get("/checkout")
def checkout_page():
    """The merchant's checkout. A form, then Razorpay's own payment screen."""
    p = ROOT / "web" / "checkout.html"
    if not p.exists():
        raise HTTPException(404, "web/checkout.html is missing")
    return FileResponse(p)


@router.get("/api/checkout/mode")
def checkout_mode():
    """Is this a real Razorpay screen or a credential-less stand-in?

    The page asks before the customer types anything and says so on screen. A demo
    that looks live and is not is worse than one that admits what it is.
    """
    live = _client() is not None
    return {"live": live, "merchant": MERCHANT_NAME,
            "abandon_minutes": sweeper.abandon_minutes(),
            "summary": ("live test mode -- Razorpay Checkout opens and the payment is "
                        "verified server-side with payment.fetch"
                        if live else
                        "no RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET -- no payment screen "
                        "can open. orders are still created and watched, and "
                        "simulate-failure shows the engine react.")}


@router.post("/api/checkout")
def create_checkout(body: dict | None = Body(None), amount: int | None = None,
                    email: str | None = None, contact: str | None = None,
                    name: str | None = None):
    """Create the order, start the abandonment clock, hand back the pay params.

    Query params override the body so this is curl-able by hand. `amount` is
    paise, like everywhere else -- 289900 is Rs 2,899.
    """
    b = body if isinstance(body, dict) else {}
    amt = amount if amount is not None else b.get("amount")
    mail = (email or b.get("email") or "").strip()
    phone = (contact or b.get("contact") or "").strip() or None
    who = (name or b.get("name") or "").strip() or None

    if not isinstance(amt, int) or amt <= 0:
        raise HTTPException(400, {
            "error": "amount must be a positive integer in paise", "got": amt,
            "why": "money is int paise everywhere in this codebase; a float is a bug"})
    if "@" not in mail:
        raise HTTPException(400, {
            "error": "a valid email is required",
            "why": "an abandoned checkout the engine cannot reach is a case it can "
                   "only ever STOP on NO_CONTACT"})

    client = _client()
    slug = mail.replace("@", "_").replace(".", "_")[-24:]

    if client is not None:
        try:
            order = client.order.create({
                "amount": amt, "currency": "INR", "receipt": f"co_{slug}"[:40],
                "notes": {"email": mail, "contact": phone or "", "name": who or "",
                          "source": "razorrecovery_checkout_demo"}})
        except Exception as e:                       # noqa: BLE001
            raise HTTPException(502, f"order.create failed: {type(e).__name__}: {e}")
        order_id, created_at = str(order.get("id")), order.get("created_at")
    else:
        order_id, created_at = f"order_demo_{slug}_{int(time.time())}", None

    # BEFORE the modal opens, not after. If the customer closes the tab there is
    # no event to react to, and a clock that starts on abandonment starts too late.
    watched = ingest_ctl.watch_order(
        con(), order_id=order_id, amount=amt, method="checkout",
        contact=phone, email=mail, name=who, receipt=f"co_{slug}"[:40],
        created_at=ingest_ctl.epoch_iso(created_at), now=datetime.now(),
        source="checkout_page")

    return {"order_id": order_id, "amount": amt, "email": mail, "name": who,
            "key": os.getenv("RAZORPAY_KEY_ID", ""), "live": client is not None,
            "merchant": MERCHANT_NAME,
            "abandon_minutes": watched["abandon_minutes"],
            "watching": watched["watching"], "watch_verdict": watched["verdict"],
            "note": ("real test-mode order -- Razorpay Checkout will open"
                     if client is not None else
                     "no RAZORPAY_KEY_ID: the order is local and no payment screen "
                     "can open. use /api/checkout/simulate-failure to see the engine "
                     "react.")}


@router.post("/api/checkout/paid")
def checkout_paid(body: dict | None = Body(None), payment_id: str | None = None,
                  order_id: str | None = None):
    """The customer paid. Verify it with Razorpay, then ingest it for real.

    The browser is a witness, not a source: it supplies the payment id and
    nothing else that matters. Everything the engine sees is read back from
    `payment.fetch`, so a forged POST here cannot invent a settlement -- an
    unknown id 502s and a payment that is not actually captured is refused below.
    """
    b = body if isinstance(body, dict) else {}
    pid = payment_id or b.get("razorpay_payment_id") or b.get("payment_id")
    oid = order_id or b.get("razorpay_order_id") or b.get("order_id")
    if not pid:
        raise HTTPException(400, "payment_id is required")

    entity = _fetch_payment(_client(), str(pid))
    status = str(entity.get("status") or "")
    if status not in ("captured", "authorized", "refunded"):
        raise HTTPException(409, {
            "error": f"payment {pid} is '{status}', not a settlement",
            "why": "only money Razorpay confirms arrived may close a debt"})

    # `authorized` is money held, not captured. Razorpay sends `payment.authorized`
    # for it; using `.captured` here would let a hold close a debt.
    event = "payment.captured" if status == "captured" else f"payment.{status}"
    out = ingest_ctl.ingest(con(), _event(event, entity, f"evt_browser_{pid}"),
                            now=datetime.now())
    return {"payment_id": pid, "order_id": oid or entity.get("order_id"),
            "status": status, "verified_with": "razorpay payment.fetch",
            "event": event, "result": out,
            "transport": "browser callback -- the payload is Razorpay's, the code "
                         "path is the webhook handler's. see the module docstring."}


@router.post("/api/checkout/failed")
def checkout_failed(body: dict | None = Body(None), payment_id: str | None = None,
                    order_id: str | None = None):
    """The payment failed. Verify it, then open the case off the real error.

    This is the interesting one: the error fields Razorpay returns are exactly
    what `ingest.classify` reads, so a test card that fails for insufficient funds
    produces a genuinely classified INSUFFICIENT_FUNDS case, not a labelled one.
    """
    b = body if isinstance(body, dict) else {}
    err = b.get("error") if isinstance(b.get("error"), dict) else {}
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    pid = payment_id or b.get("payment_id") or meta.get("payment_id")
    oid = order_id or b.get("order_id") or meta.get("order_id")

    if not pid:
        # Razorpay declined before a payment existed (a validation error). There
        # is nothing to fetch and nothing to classify; the order stays watched and
        # the sweeper will deal with the silence.
        return {"ingested": False, "order_id": oid, "error": err,
                "verdict": "no payment id -- Razorpay rejected the attempt before a "
                           "payment existed. the order stays on the watch list."}

    entity = _fetch_payment(_client(), str(pid))
    out = ingest_ctl.ingest(con(),
                            _event("payment.failed", entity, f"evt_browser_{pid}"),
                            now=datetime.now())
    return {"payment_id": pid, "order_id": oid or entity.get("order_id"),
            "status": entity.get("status"),
            "error_reason": entity.get("error_reason"),
            "error_description": entity.get("error_description"),
            "verified_with": "razorpay payment.fetch", "result": out}


@router.post("/api/checkout/simulate-failure")
def simulate_failure(body: dict | None = Body(None), order_id: str | None = None):
    """Build a `payment.failed` for a watched order and run it through ingest.

    THE ONE ENDPOINT HERE THAT INVENTS A PAYLOAD, and it says so in its response.
    It exists because the live path needs credentials and a network, and a demo
    that cannot run offline is a demo that does not run. The event it builds is
    the same shape as `fixtures/webhooks/01_payment_failed_insufficient_funds.json`
    and it goes through the real `ingest.ingest`, so the case, the classification,
    the gates and the dashboard are all genuine -- only the payment is not.
    """
    b = body if isinstance(body, dict) else {}
    oid = order_id or b.get("order_id")
    if not oid:
        raise HTTPException(400, "order_id is required")
    reason = str(b.get("reason") or "insufficient_funds")
    row = _row(str(oid))

    entity = {
        "id": f"pay_sim_{oid}"[:40], "entity": "payment", "amount": row["amount"],
        "currency": "INR", "status": "failed", "order_id": oid, "method": "card",
        "captured": False, "customer_id": row["customer_id"],
        "email": row["email"], "contact": row["contact"],
        "error_code": "BAD_REQUEST_ERROR", "error_reason": reason,
        "error_source": "bank", "error_step": "payment_authorization",
        "error_description": "Simulated failure -- no payment was attempted.",
        "created_at": int(time.time()),
    }
    out = ingest_ctl.ingest(con(), _event("payment.failed", entity,
                                          f"evt_sim_{oid}"), now=datetime.now())
    return {"order_id": oid, "amount": row["amount"], "simulated": True,
            "error_reason": reason, "result": out,
            "honesty": "this payload was built by this endpoint, not by Razorpay. "
                       "the case, the classification and the decision are real; "
                       "the payment is not."}
