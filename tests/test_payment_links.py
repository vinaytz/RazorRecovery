"""
Live payment links: the order rule, idempotency, expiry, and graceful degradation.

The first test in this file is the one that matters most. Everything else here is
about not sending the same link twice; that one is about not breaking the
merchant's fulfilment.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.repos import store
from app.services.clock import VirtualClock
from app.services.executor import (
    RazorpayExecutor,
    build_executor,
    obligation_from_reference_id,
    reference_id_for,
)

NOW = datetime(2026, 3, 10, 12, 0, 0)


class FakeClient:
    """Records every call. Any attribute we do not expect raises on use."""

    def __init__(self, link_id: str = "plink_1"):
        self.link_id = link_id
        self.payloads: list[dict] = []
        self.order_creates = 0

        client = self

        class _PaymentLink:
            def create(self, data):
                client.payloads.append(data)
                return {"id": client.link_id, "status": "created",
                        "short_url": f"https://rzp.io/i/{client.link_id}",
                        "reference_id": data["reference_id"]}

        class _Order:
            def create(self, data):
                client.order_creates += 1
                return {"id": "order_WE_SHOULD_NEVER_MINT_THIS"}

        self.payment_link = _PaymentLink()
        self.order = _Order()


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    c.execute("INSERT INTO obligations (id, customer_id, amount_due, amount_settled,"
              " status, opened_at, contact, email) VALUES (?,?,?,?,?,?,?,?)",
              ("order_MerchantABC123", "cust1", 500_000, 0, "OPEN", NOW.isoformat(),
               "+919876543210", "asha@example.com"))
    c.commit()
    return c


def live(con, client=None, dry_run=False):
    return RazorpayExecutor(client or FakeClient(), con=con, dry_run=dry_run,
                            clock=VirtualClock(NOW), window_hours=168,
                            merchant_name="ExampleMart")


# -- the order rule --------------------------------------------------------

def test_never_creates_an_order(con):
    """The single most important assertion in the live path.

    If we mint our own order, the customer's money lands against an id the
    merchant's backend has never seen, and the goods never ship.
    """
    client = FakeClient()
    live(con, client).create_payment_link("order_MerchantABC123")
    assert client.order_creates == 0


def test_payload_carries_the_merchants_order_id(con):
    client = FakeClient()
    r = live(con, client).create_payment_link("order_MerchantABC123")
    payload = client.payloads[0]
    assert payload["notes"]["order_id"] == "order_MerchantABC123"
    assert payload["reference_id"].startswith("order_MerchantABC123")
    assert obligation_from_reference_id(r["reference_id"]) == "order_MerchantABC123"


def test_non_order_debt_carries_no_fabricated_order_id(con):
    con.execute("INSERT INTO obligations (id, customer_id, amount_due, amount_settled,"
                " status, opened_at) VALUES (?,?,?,?,?,?)",
                ("inv_9", "cust1", 100_000, 0, "OPEN", NOW.isoformat()))
    con.commit()
    client = FakeClient()
    live(con, client).create_payment_link("inv_9")
    # Empty, never invented.
    assert client.payloads[0]["notes"]["order_id"] == ""


# -- payload shape ---------------------------------------------------------

def test_payload_matches_the_api_contract(con):
    client = FakeClient()
    live(con, client).create_payment_link("order_MerchantABC123")
    p = client.payloads[0]

    assert p["amount"] == 500_000                     # outstanding, in paise
    assert p["currency"] == "INR"
    assert len(p["reference_id"]) <= 40               # Razorpay's documented cap
    assert isinstance(p["expire_by"], int)            # epoch SECONDS, not an ISO string
    assert p["expire_by"] == int((NOW + timedelta(hours=168)).timestamp())
    assert p["expire_by"] > int((NOW + timedelta(minutes=15)).timestamp())
    assert p["notify"] == {"sms": False, "email": False}   # we notify, not Razorpay
    assert p["reminder_enable"] is False
    assert p["customer"] == {"contact": "+919876543210", "email": "asha@example.com"}
    assert len(p["notes"]) <= 15
    # No unknown keys: the API rejects extras with a 400.
    assert set(p) <= {"amount", "currency", "description", "reference_id", "expire_by",
                      "notify", "reminder_enable", "notes", "customer", "accept_partial",
                      "first_min_partial_amount", "upi_link", "callback_url",
                      "callback_method"}
    assert "order_id" not in p                        # no such parameter exists


def test_amount_is_the_outstanding_balance_not_the_original(con):
    con.execute("UPDATE obligations SET amount_settled = 200_000 WHERE id = ?",
                ("order_MerchantABC123",))
    con.commit()
    client = FakeClient()
    live(con, client).create_payment_link("order_MerchantABC123")
    assert client.payloads[0]["amount"] == 300_000


def test_expire_by_is_floored_when_the_window_has_nearly_closed(con):
    client = FakeClient()
    ex = RazorpayExecutor(client, con=con, dry_run=False, clock=VirtualClock(NOW),
                          window_hours=168)
    ex.create_payment_link("order_MerchantABC123",
                           window_ends_at=NOW + timedelta(minutes=3))
    # The API rejects anything under 15 minutes out, so we clamp rather than fail.
    assert client.payloads[0]["expire_by"] >= int((NOW + timedelta(minutes=15)).timestamp())


def test_malformed_contact_is_dropped_not_sent(con):
    con.execute("UPDATE obligations SET contact = '12' WHERE id = ?",
                ("order_MerchantABC123",))
    con.commit()
    client = FakeClient()
    live(con, client).create_payment_link("order_MerchantABC123")
    # Razorpay requires 8-14 chars. Losing the field beats losing the whole link.
    assert "contact" not in client.payloads[0]["customer"]


def test_reference_id_stays_under_forty_characters():
    ref = reference_id_for("order_" + "x" * 60, "METHOD_CHANGE")
    assert len(ref) <= 40
    assert ref.startswith("order_")


# -- idempotency -----------------------------------------------------------

def test_same_obligation_and_action_returns_the_same_link(con):
    client = FakeClient()
    ex = live(con, client)
    first = ex.create_payment_link("order_MerchantABC123")
    second = ex.create_payment_link("order_MerchantABC123")

    assert len(client.payloads) == 1                  # one API call, not two
    assert first["short_url"] == second["short_url"]
    assert first["reused"] is False and second["reused"] is True


def test_a_different_action_gets_its_own_link(con):
    client = FakeClient()
    ex = live(con, client)
    ex.create_payment_link("order_MerchantABC123", action="PAY_LINK")
    ex.create_payment_link("order_MerchantABC123", action="METHOD_CHANGE")
    assert len(client.payloads) == 2
    refs = {p["reference_id"] for p in client.payloads}
    assert len(refs) == 2                             # Razorpay's uniqueness rule holds


def test_an_expired_link_is_replaced_not_reused(con):
    client = FakeClient()
    clock = VirtualClock(NOW)
    ex = RazorpayExecutor(client, con=con, dry_run=False, clock=clock, window_hours=168)
    ex.create_payment_link("order_MerchantABC123")
    clock.advance(hours=200)                          # past the 168h window
    ex.create_payment_link("order_MerchantABC123")
    assert len(client.payloads) == 2


def test_a_paid_link_is_not_reused(con):
    client = FakeClient()
    ex = live(con, client)
    ex.create_payment_link("order_MerchantABC123")
    store.mark_payment_link(con, "order_MerchantABC123", "paid")
    con.execute("UPDATE obligations SET amount_settled = 0 WHERE id = ?",
                ("order_MerchantABC123",))
    con.commit()
    ex.create_payment_link("order_MerchantABC123")
    assert len(client.payloads) == 2


def test_nothing_outstanding_sends_nothing(con):
    con.execute("UPDATE obligations SET amount_settled = amount_due, status = 'SETTLED'"
                " WHERE id = ?", ("order_MerchantABC123",))
    con.commit()
    client = FakeClient()
    r = live(con, client).create_payment_link("order_MerchantABC123")
    assert r["ok"] is False and r["detail"] == "NOTHING_OUTSTANDING"
    assert client.payloads == []


# -- degradation -----------------------------------------------------------

def test_dry_run_builds_the_payload_but_sends_nothing(con):
    client = FakeClient()
    r = RazorpayExecutor(client, con=con, dry_run=True, clock=VirtualClock(NOW)
                         ).create_payment_link("order_MerchantABC123")
    assert client.payloads == []                      # the API was never called
    assert r["mode"] == "dry_run" and r["ok"] is True
    assert r["payload"]["notes"]["order_id"] == "order_MerchantABC123"


def test_dry_run_is_still_idempotent(con):
    ex = RazorpayExecutor(FakeClient(), con=con, dry_run=True, clock=VirtualClock(NOW))
    a = ex.create_payment_link("order_MerchantABC123")
    b = ex.create_payment_link("order_MerchantABC123")
    assert a["short_url"] == b["short_url"] and b["reused"] is True


def test_no_credentials_returns_a_stub_and_does_not_crash(con):
    r = RazorpayExecutor(None, con=con, dry_run=False, clock=VirtualClock(NOW)
                         ).create_payment_link("order_MerchantABC123")
    assert r["mode"] == "stub_no_key" and r["ok"] is True
    assert r["short_url"].startswith("https://rzp.invalid/")


def test_api_error_is_a_failed_result_not_an_exception(con):
    class Boom(FakeClient):
        def __init__(self):
            super().__init__()
            outer = self

            class _PaymentLink:
                def create(self, data):
                    raise RuntimeError("BAD_REQUEST_ERROR: reference id already attempted")
            self.payment_link = _PaymentLink()

    r = live(con, Boom()).create_payment_link("order_MerchantABC123")
    assert r["ok"] is False and "BAD_REQUEST_ERROR" in r["detail"]


def test_build_executor_defaults_to_dry_run(monkeypatch, con):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    ex = build_executor(con)
    assert ex.dry_run is True and ex.client is None   # stub, loudly, never a crash


# -- the port still works --------------------------------------------------

def test_execute_routes_link_actions_through_create_payment_link(con):
    """Every contact action mints its own link and then sends it."""
    from app.services.notifier import CounterNotifier

    client = FakeClient()
    notifier = CounterNotifier()
    ex = live(con, client)
    ex.notifier = notifier

    for action in ("REMIND", "PAY_LINK", "METHOD_CHANGE"):
        res = ex.execute(action, "order_MerchantABC123", f"idem_{action}")
        assert res.ok, res.detail
        assert res.contact_sent is True
    assert len(client.payloads) == 3
    assert notifier.count == 3
    # The link the customer receives is the one we minted, in the body.
    assert "rzp.io" in notifier.sent[0]["body"]


def test_a_link_with_nowhere_to_send_it_is_a_failure_not_a_success(con):
    """A minted link nobody received is not a recovery attempt.

    Live, with no notifier configured: the link exists but the customer does not
    know about it. Reporting `ok` here would log a contact that never happened and
    burn the customer's 7-day cap (G13) on silence.
    """
    client = FakeClient()
    res = live(con, client).execute("REMIND", "order_MerchantABC123", "idem_x")
    assert res.ok is False
    assert res.contact_sent is False
    assert "NO_NOTIFIER_CONFIGURED" in res.detail
    assert len(client.payloads) == 1        # the link was still minted, not lost


def test_dry_run_mints_nothing_and_sends_nothing(con):
    from app.services.notifier import CounterNotifier

    notifier = CounterNotifier()
    ex = live(con, FakeClient(), dry_run=True)
    ex.notifier = notifier
    res = ex.execute("PAY_LINK", "order_MerchantABC123", "idem_dry")
    assert res.ok is True and res.contact_sent is False
    assert notifier.count == 0              # DRY_RUN=true is the default for a reason
    assert "nothing sent" in res.detail


def test_execute_never_debits_on_retry(con):
    client = FakeClient()
    r = live(con, client).execute("RETRY", "order_MerchantABC123", "idem_r")
    assert r.ok is False and "INTENT_ONLY" in r.detail
    assert client.payloads == []
