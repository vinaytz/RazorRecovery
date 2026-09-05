"""
The /checkout page and the three endpoints behind it.

It is the one surface where a customer pays, so it is also the one place where a
bug would be invisible from the dashboard: the dashboard numbers would be right
and nothing would have ever reached them. What is pinned here:

  the page loads            GET /checkout returns the static form
  the order is watched      /api/checkout starts the clock BEFORE the modal opens
  the feed is one database  a checkout writes where the dashboard reads
  the mode is honest        the page states live vs credential-less up front
  money stays int paise     a float amount is rejected, not coerced
  the simulated failure     is labelled as simulated and still runs the real
                            ingest path, so the case is real, only the payment
                            is not
  a payment can't be forged a /paid callback is refused without credentials and
                            refuses to close a debt unless Razorpay confirms it

The sandbox has no Razorpay credentials, so no test here touches the live SDK
path. That is the one gap, and it is deliberate: the SDK path needs a real
account and is exercised by hand in the demo, while these tests pin the shapes
and the integrity of everything around it.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dashboard
from app.repos import store
from app.workers import sweeper

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    con = store.connect(tmp_path / "t.db")
    store.init(con)
    monkeypatch.setattr(dashboard, "_con", con)
    import main
    return TestClient(main.app), con


def checkout(email="rohit@example.com", amount=289_900, name="Rohit", **extra):
    return {"email": email, "amount": amount, "name": name, **extra}


# -- the page ---------------------------------------------------------------

def test_checkout_page_loads(client):
    c, _ = client
    r = c.get("/checkout")
    assert r.status_code == 200
    assert b"Razorpay" in r.content or b"Acme Retail" in r.content


def test_mode_endpoint_is_honest_without_credentials(client):
    c, _ = client
    m = c.get("/api/checkout/mode").json()
    assert m["live"] is False
    assert "no RAZORPAY_KEY_ID" in m["summary"]


# -- creating the order -----------------------------------------------------

def test_create_watches_before_returning(client):
    c, con = client
    r = c.post("/api/checkout", json=checkout())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["watching"] is True
    # The order is on the watch list already -- a customer who closes the modal
    # does not need any further call for the sweeper to know about them.
    row = con.execute("SELECT status, amount, email FROM checkouts"
                      " WHERE order_id = ?", (d["order_id"],)).fetchone()
    assert row["status"] == "WATCHING"
    assert row["amount"] == 289_900
    assert row["email"] == "rohit@example.com"
    # And no case was opened -- a customer on the payment page is not a target.
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_amount_is_int_paise_only(client):
    c, _ = client
    for bad in (0, -100, 29.5, "289900", None):
        r = c.post("/api/checkout", json=checkout(amount=bad))
        assert r.status_code == 400, (bad, r.text)


def test_email_is_required_and_checked(client):
    c, _ = client
    assert c.post("/api/checkout", json=checkout(email="nope")).status_code == 400
    assert c.post("/api/checkout", json={**checkout(), "email": ""}).status_code == 400


def test_checkout_writes_to_the_dashboard_database(client):
    c, con = client
    c.post("/api/checkout", json=checkout())
    counts = con.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0]
    assert counts == 1
    # /api/checkouts reads through dashboard.con() -- the same connection.
    out = c.get("/api/checkouts").json()
    assert len(out["watching"]) == 1


def test_a_second_checkout_does_not_reset_the_first(client):
    c, con = client
    oid = c.post("/api/checkout", json=checkout()).json()["order_id"]
    c.post("/api/checkout", json=checkout(email="other@example.com"))
    assert con.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 2
    # The first is still WATCHING, untouched by the second.
    row = con.execute("SELECT status FROM checkouts WHERE order_id = ?", (oid,)).fetchone()
    assert row["status"] == "WATCHING"


# -- the simulated failure --------------------------------------------------

def test_simulate_failure_opens_a_real_case(client):
    c, con = client
    oid = c.post("/api/checkout", json=checkout()).json()["order_id"]
    r = c.post("/api/checkout/simulate-failure", json={"order_id": oid})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["simulated"] is True
    assert d["error_reason"] == "insufficient_funds"
    row = con.execute("SELECT case_id, failure_class, amount, status, kind"
                      " FROM cases").fetchone()
    assert row["failure_class"] == "INSUFFICIENT_FUNDS"
    assert row["amount"] == 289_900
    assert row["status"] == "OPEN"
    # A failed payment is an ORDER, not a CHECKOUT-abandonment kind.
    assert row["kind"] == "ORDER"


def test_simulate_needs_an_order_created_here_first(client):
    c, _ = client
    r = c.post("/api/checkout/simulate-failure",
               json={"order_id": "order_nope"})
    assert r.status_code == 404


def test_an_abandoned_checkout_that_also_failed_does_not_double_count(client):
    """A failure opens an ORDER case. The same order left unpaid must not be
    swept into a second case for the same money, and the first is not rewritten."""
    c, con = client
    oid = c.post("/api/checkout", json=checkout()).json()["order_id"]
    c.post("/api/checkout/simulate-failure", json={"order_id": oid})
    sweeper.sweep(con, datetime.now(), minutes=0)
    rows = con.execute("SELECT failure_class, kind, status FROM cases").fetchall()
    assert len(rows) == 1, rows
    # The abandonment sweep did not overwrite the failure case.
    assert rows[0]["failure_class"] == "INSUFFICIENT_FUNDS"
    assert rows[0]["kind"] == "ORDER"


def test_abandoned_without_payment_becomes_checkout_case(client):
    c, con = client
    oid = c.post("/api/checkout", json=checkout()).json()["order_id"]
    out = sweeper.sweep(con, datetime.now(), minutes=0)
    assert len(out["opened"]) == 1
    row = con.execute("SELECT failure_class, kind, status FROM cases").fetchone()
    assert row["failure_class"] == "CHECKOUT_ABANDONED"
    assert row["kind"] == "CHECKOUT"
    r = con.execute("SELECT status FROM checkouts WHERE order_id = ?", (oid,)).fetchone()
    assert r["status"] == "ABANDONED"


# -- the verify-from-razorpay callbacks -------------------------------------

def test_paid_without_credentials_is_refused(client):
    c, _ = client
    r = c.post("/api/checkout/paid", json={"razorpay_payment_id": "pay_whatever"})
    assert r.status_code == 400
    assert "credentials" in r.json()["detail"]["error"]


def test_paid_requires_a_payment_id(client):
    c, _ = client
    assert c.post("/api/checkout/paid", json={}).status_code == 400
