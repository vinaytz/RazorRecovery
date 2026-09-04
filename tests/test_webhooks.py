"""
P7: the live webhook path.

The two things that must hold:
  - a bad signature is rejected, and verification happens on the RAW body
  - a success event closes the case, so we never chase someone who already paid
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import webhooks
from app.controllers import ingest as ing
from app.repos import store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"
SECRET = "test_webhook_secret_123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    con = store.connect(tmp_path / "t.db")
    store.init(con)
    monkeypatch.setattr(webhooks, "_con", con)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    import main
    return TestClient(main.app), con


def sign(raw: bytes, key: str = SECRET) -> str:
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def post(c, payload: dict, key: str | None = SECRET):
    raw = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if key:
        headers["x-razorpay-signature"] = sign(raw, key)
    return c.post("/webhooks/razorpay", content=raw, headers=headers)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# -- signature -------------------------------------------------------------

def test_bad_signature_rejected(client):
    c, _ = client
    r = post(c, load("01_payment_failed_insufficient_funds.json"), key="wrong_secret")
    assert r.status_code == 400


def test_missing_signature_rejected(client):
    c, _ = client
    r = post(c, load("01_payment_failed_insufficient_funds.json"), key=None)
    assert r.status_code == 400


def test_signature_is_over_raw_body_not_reserialised(client):
    """Same object, different byte encoding -> the raw bytes are what count."""
    c, _ = client
    payload = load("01_payment_failed_insufficient_funds.json")
    compact = json.dumps(payload, separators=(",", ":")).encode()
    spaced = json.dumps(payload, indent=2).encode()
    assert sign(compact) != sign(spaced)
    r = c.post("/webhooks/razorpay", content=compact,
               headers={"x-razorpay-signature": sign(compact),
                        "content-type": "application/json"})
    assert r.status_code == 200


# -- ingest ----------------------------------------------------------------

def test_failure_opens_case_and_classifies(client):
    c, con = client
    r = post(c, load("01_payment_failed_insufficient_funds.json"))
    assert r.status_code == 200
    b = r.json()
    assert b["signature_verified"] is True
    assert b["action"] == "case_opened"
    assert b["failure_class"] == "INSUFFICIENT_FUNDS"
    assert b["amount"] == 499900
    assert b["obligation_id"] == "order_TEST000000001"   # order, not payment id

    row = con.execute("SELECT * FROM cases WHERE case_id = ?", (b["case_id"],)).fetchone()
    assert row["status"] == "OPEN"


def test_duplicate_delivery_is_ignored(client):
    c, con = client
    p = load("01_payment_failed_insufficient_funds.json")
    first, second = post(c, p), post(c, p)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 1


def test_success_closes_the_case(client):
    """The classic bug: a recovery engine that only listens for failures."""
    c, con = client
    opened = post(c, load("01_payment_failed_insufficient_funds.json")).json()
    r = post(c, load("03_payment_captured_settles_case.json")).json()

    assert r["action"] == "case_closed"
    assert r["cases_closed"] == 1
    row = con.execute("SELECT status FROM cases WHERE case_id = ?",
                      (opened["case_id"],)).fetchone()
    assert row["status"] == "SELF_RECOVERED"


def test_success_cancels_pending_actions(client):
    from datetime import datetime

    from app.controllers import execute as ex
    from app.domain.models import ActionType

    c, con = client
    opened = post(c, load("01_payment_failed_insufficient_funds.json")).json()
    ex.schedule(con, opened["case_id"], opened["obligation_id"],
                ActionType.PAY_LINK, datetime(2026, 3, 10, 12, 0))

    r = post(c, load("03_payment_captured_settles_case.json")).json()
    assert r["actions_cancelled"] == 1
    st = con.execute("SELECT status FROM actions WHERE obligation_id = ?",
                     (opened["obligation_id"],)).fetchone()["status"]
    assert st == "CANCELLED"


def test_expired_card_classified(client):
    c, _ = client
    b = post(c, load("02_payment_failed_card_expired.json")).json()
    assert b["failure_class"] == "CARD_EXPIRED"


def test_subscription_halted_keys_on_subscription(client):
    c, _ = client
    b = post(c, load("04_subscription_halted_mandate.json")).json()
    assert b["obligation_id"] == "sub_TEST000000001"
    assert b["failure_class"] == "MANDATE_INVALID"


def test_downtime_event_stored_not_a_case(client):
    c, con = client
    b = post(c, load("05_payment_downtime_started.json")).json()
    assert b["action"] == "noted"
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_unknown_event_type_does_not_500(client):
    c, _ = client
    r = post(c, {"event": "payment.dispute.created", "id": "evt_x", "payload": {}})
    assert r.status_code == 200
    assert r.json()["action"] == "stored_only"


def test_malformed_body_is_400_not_500(client):
    c, _ = client
    raw = b"{not json"
    r = c.post("/webhooks/razorpay", content=raw,
               headers={"x-razorpay-signature": sign(raw)})
    assert r.status_code == 400


# -- fixture replay --------------------------------------------------------

def test_replay_all_fixtures(client):
    c, con = client
    r = c.post("/webhooks/razorpay/replay")
    assert r.status_code == 200
    b = r.json()
    assert b["replayed"] == 7
    assert all(x["signature_verified"] for x in b["results"])


def test_seven_fixtures_on_disk():
    assert len(sorted(FIXTURES.glob("*.json"))) == 7


# -- classifier ------------------------------------------------------------

@pytest.mark.parametrize("reason,expected", [
    ("insufficient_funds", "INSUFFICIENT_FUNDS"),
    ("card_expired", "CARD_EXPIRED"),
    ("payment_method_blocked", "CARD_BLOCKED"),
    ("invalid_mandate", "MANDATE_INVALID"),
    ("otp_attempts_exceeded", "AUTH_ABANDONED"),
    ("bank_down", "ISSUER_DOWN"),
    ("something_we_never_saw", "UNKNOWN"),
])
def test_classify_reasons(reason, expected):
    payload = {"payload": {"payment": {"entity": {"error_reason": reason}}}}
    assert ing.classify(payload).value == expected
