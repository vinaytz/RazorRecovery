"""
The merchant-side integration that feeds abandonment detection.

WHY THIS FILE EXISTS. The abandoned-checkout sweeper was fed by an
`order.created` webhook. Razorpay does not emit one -- it is not in their webhook
event list, because order creation is a server-side call the MERCHANT makes and a
gateway cannot announce something it did not observe. So the entire
absence-detection source could never have fired in production: no `order.created`
delivery means no WATCHING row means the sweeper sweeps an empty table forever.
The unit tests all passed, because they all fed it a hand-built `order.created`
payload.

That is the same defect class as a metric that can only be zero: an instrument
that only ever runs on its own fixture is not evidence that it works.

The fix is `POST /api/orders/watch`, called by the merchant's backend right after
`orders.create()`. What is pinned here:

  the production path stands alone   abandonment works with NO webhook at all
  the integration is one line        POST the order object from orders.create()
  the clock starts at creation       not at receipt, so a late call is not granted grace
  a retry is not a reset             at-least-once delivery cannot defer forever
  it opens no case and sends nothing a WATCHING row is not yet at-risk revenue
  both feeds agree                   fixture replay and the API produce the same row

`test_abandonment_needs_no_order_created_webhook` is the one that matters. If
someone deletes the endpoint and goes back to webhook-only, it fails.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dashboard
from app.controllers import ingest as ing
from app.repos import store
from app.workers import sweeper

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "webhooks"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    con = store.connect(tmp_path / "t.db")
    store.init(con)
    monkeypatch.setattr(dashboard, "_con", con)
    import main
    return TestClient(main.app), con


def order_object(order_id="order_MERCH1", amount=289_900, minutes_ago=0, **extra):
    """What `client.order.create({...})` actually hands back to the merchant.

    Field names and shape are Razorpay's Orders API response, not our own -- the
    point of the endpoint is that a merchant posts this verbatim with no mapping.
    """
    created = datetime.now() - timedelta(minutes=minutes_ago)
    return {"id": order_id, "entity": "order", "amount": amount, "amount_paid": 0,
            "amount_due": amount, "currency": "INR", "receipt": f"rcpt_{order_id}",
            "status": "created", "attempts": 0,
            "created_at": int(created.timestamp()), **extra}


# -- the one that matters --------------------------------------------------

def test_abandonment_needs_no_order_created_webhook(client):
    """The whole source works with zero webhook deliveries. This is the fix.

    Nothing in this test touches `/webhooks/razorpay` or an `order.created`
    payload. If abandonment detection ever depends on one again, this fails.
    """
    c, con = client
    r = c.post("/api/orders/watch", json=order_object(minutes_ago=45))
    assert r.status_code == 200, r.text
    assert r.json()["watching"] is True

    # No case yet -- the sweeper has not run.
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0

    out = sweeper.sweep(con, datetime.now(), minutes=30)
    assert len(out["opened"]) == 1, out

    row = con.execute("SELECT case_id, failure_class, kind, amount, status"
                      " FROM cases").fetchone()
    assert row["failure_class"] == "CHECKOUT_ABANDONED"
    assert row["amount"] == 289_900
    assert row["status"] == "OPEN"


def test_the_only_watch_list_writer_is_reachable_without_a_webhook(client):
    """Guards the seam, not just the behaviour.

    `store.watch_checkout` had exactly one caller and it lived behind
    `WATCH_EVENTS`. Both feeds now go through `ingest.watch_order`, so this asserts
    the shared entry point exists and that the webhook set is documented as
    fixture-only rather than load-bearing.
    """
    assert callable(ing.watch_order)
    src = (ROOT / "app" / "controllers" / "ingest.py").read_text()
    assert "RAZORPAY DOES NOT EMIT `order.created`" in src, (
        "the reason this endpoint exists must stay written next to WATCH_EVENTS")


# -- the integration is one line ------------------------------------------

def test_posting_the_raw_order_object_needs_no_mapping(client):
    c, con = client
    r = c.post("/api/orders/watch", json=order_object(order_id="order_RAW"))
    assert r.status_code == 200, r.text

    row = con.execute("SELECT * FROM checkouts WHERE order_id = 'order_RAW'").fetchone()
    assert row["status"] == "WATCHING"
    assert row["amount"] == 289_900
    assert row["receipt"] == "rcpt_order_RAW"


def test_notes_carry_reachability(client):
    """A merchant's checkout integration already puts contact details in `notes`.

    Without this the sweeper opens a case for a customer with no address, and G13
    burns a contact slot discovering that.
    """
    c, con = client
    r = c.post("/api/orders/watch", json=order_object(
        order_id="order_NOTES",
        notes={"contact": "+919812345678", "email": "a@b.com", "name": "Rohit Sharma"}))
    assert r.status_code == 200, r.text

    row = con.execute("SELECT * FROM checkouts WHERE order_id = 'order_NOTES'").fetchone()
    assert row["contact"] == "+919812345678"
    assert row["email"] == "a@b.com"
    assert row["name"] == "Rohit Sharma"


def test_query_params_work_for_a_hand_rolled_curl(client):
    c, con = client
    r = c.post("/api/orders/watch?order_id=order_CURL&amount=500000&email=x@y.com")
    assert r.status_code == 200, r.text
    row = con.execute("SELECT * FROM checkouts WHERE order_id = 'order_CURL'").fetchone()
    assert row["amount"] == 500_000
    assert row["email"] == "x@y.com"


# -- the clock ------------------------------------------------------------

def test_the_clock_starts_at_order_creation_not_at_the_call(client):
    """A merchant whose queue was backed up does not get a fresh window.

    `created_at` on the order is epoch seconds. An order created 45 minutes ago is
    already past a 30-minute window the instant we hear about it.
    """
    c, con = client
    c.post("/api/orders/watch", json=order_object(order_id="order_LATE", minutes_ago=45))
    assert len(sweeper.sweep(con, datetime.now(), minutes=30)["opened"]) == 1


def test_a_fresh_order_is_not_at_risk(client):
    """The customer is still looking at the payment page. Chasing them now is the
    most expensive false chase there is."""
    c, con = client
    c.post("/api/orders/watch", json=order_object(order_id="order_FRESH", minutes_ago=0))
    assert len(sweeper.sweep(con, datetime.now(), minutes=30)["opened"]) == 0
    row = con.execute("SELECT status FROM checkouts WHERE order_id='order_FRESH'").fetchone()
    assert row["status"] == "WATCHING"


def test_no_created_at_falls_back_to_now_not_to_the_epoch(client):
    """Missing timestamp means wait longer, never chase sooner.

    `datetime.fromtimestamp(0)` would put the order 56 years in the past and open a
    case on the next sweep.
    """
    c, con = client
    body = order_object(order_id="order_NOTS")
    body.pop("created_at")
    c.post("/api/orders/watch", json=body)
    assert len(sweeper.sweep(con, datetime.now(), minutes=30)["opened"]) == 0


def test_a_retried_call_does_not_reset_the_clock(client):
    """At-least-once delivery from the merchant's side must not defer forever."""
    c, con = client
    first = c.post("/api/orders/watch", json=order_object(order_id="order_DUP",
                                                          minutes_ago=45)).json()
    again = c.post("/api/orders/watch", json=order_object(order_id="order_DUP",
                                                          minutes_ago=0)).json()
    assert first["fresh"] is True
    assert again["fresh"] is False
    assert "not reset" in again["verdict"]

    assert con.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 1
    # The original 45-minute-old timestamp survived, so the window still closes.
    assert len(sweeper.sweep(con, datetime.now(), minutes=30)["opened"]) == 1


# -- it opens nothing and sends nothing -----------------------------------

def test_watching_opens_no_case_and_no_action(client):
    c, con = client
    c.post("/api/orders/watch", json=order_object(order_id="order_QUIET"))
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 0


def test_payment_takes_the_order_off_the_watch_list(client):
    """The engine must never chase a checkout that completed."""
    c, con = client
    c.post("/api/orders/watch", json=order_object(order_id="order_PAID", minutes_ago=45))
    ing.ingest(con, {"event": "payment.captured", "id": "evt_p1", "payload": {"payment": {
        "entity": {"id": "pay_1", "amount": 289_900, "status": "captured",
                   "method": "upi", "captured": True, "order_id": "order_PAID"}}}},
        now=datetime.now())

    row = con.execute("SELECT status FROM checkouts WHERE order_id='order_PAID'").fetchone()
    assert row["status"] == "PAID"
    assert len(sweeper.sweep(con, datetime.now(), minutes=30)["opened"]) == 0


# -- bad input is refused with a reason -----------------------------------

def test_no_order_id_is_a_400_that_explains_the_integration(client):
    c, _ = client
    r = c.post("/api/orders/watch", json={"amount": 100_000})
    assert r.status_code == 400
    assert "order-created webhook" in json.dumps(r.json())


def test_a_zero_or_float_amount_is_refused(client):
    c, con = client
    assert c.post("/api/orders/watch",
                  json={"id": "order_X", "amount": 0}).status_code == 400
    assert c.post("/api/orders/watch",
                  json={"id": "order_X", "amount": 2899.0}).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 0


# -- the two feeds agree --------------------------------------------------

def test_fixture_replay_and_the_api_produce_the_same_watch_row(client):
    """The demo path and the production path must not drift.

    Fixture 08 stays runnable -- the judge presses a button and sees an order
    appear -- but it is a replay of an event Razorpay never sends, and the row it
    writes has to be identical to the one a real merchant call writes.
    """
    c, con = client
    payload = json.loads((FIXTURES / "08_order_created_then_abandoned.json").read_text())
    ing.ingest(con, payload, now=datetime.now())
    oid = ing.obligation_id_of(payload)
    via_webhook = dict(con.execute(
        "SELECT * FROM checkouts WHERE order_id = ?", (oid,)).fetchone())

    order = payload["payload"]["order"]["entity"]
    c.post("/api/orders/watch", json={**order, "id": "order_VIA_API"})
    via_api = dict(con.execute(
        "SELECT * FROM checkouts WHERE order_id = 'order_VIA_API'").fetchone())

    ignore = {"order_id", "customer_id", "receipt", "seen_at"}
    for key in via_webhook:
        if key in ignore:
            continue
        assert via_api[key] == via_webhook[key], f"{key} drifted between the two feeds"
