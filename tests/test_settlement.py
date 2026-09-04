"""
The settlement loop and the match ladder.

Two things are being pinned here. The first is that money finds its debt. The
second, and the one that matters more, is that we never claim more certainty than
we have: a level-4 amount match must arrive at the dashboard labelled a heuristic,
and an ambiguous one must close nothing at all.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.controllers import ingest as ing
from app.repos import store
from app.services import matcher

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"
NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


def obligation(c, oid, *, amount=499_900, customer="cust1", contact=None, email=None,
               opened=None, status="OPEN"):
    c.execute("INSERT OR REPLACE INTO obligations (id, customer_id, amount_due,"
              " amount_settled, status, opened_at, contact, email, name)"
              " VALUES (?,?,?,?,?,?,?,?,?)",
              (oid, customer, amount, 0, status, (opened or NOW).isoformat(),
               contact, email, None))
    c.execute("INSERT OR REPLACE INTO cases (case_id, run_id, obligation_id, customer_id,"
              " arm, amount, failure_class, method, kind, rung, attempts, status,"
              " contacts_sent, actions_taken, opened_at, closed_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (f"live_{oid}", "live", oid, customer, "ENGINE", amount,
               "INSUFFICIENT_FUNDS", "card", "ORDER", 0, 1, "OPEN", 0, 0,
               (opened or NOW).isoformat(), None))
    c.commit()
    return f"live_{oid}"


def captured(*, payment_id="pay_1", amount=499_900, order_id=None, customer_id=None,
             contact=None, email=None, notes=None, event="payment.captured"):
    entity = {"id": payment_id, "amount": amount, "status": "captured",
              "method": "upi", "captured": True}
    if order_id:
        entity["order_id"] = order_id
    if customer_id:
        entity["customer_id"] = customer_id
    if contact:
        entity["contact"] = contact
    if email:
        entity["email"] = email
    if notes is not None:
        entity["notes"] = notes
    return {"event": event, "id": f"evt_{payment_id}_{event}",
            "payload": {"payment": {"entity": entity}}}


def our_link_notes(oid, action="PAY_LINK"):
    return {"obligation_id": oid, "order_id": oid, "recovery_action": action,
            "source": "razorrecovery"}


# -- level 1: an id we already track --------------------------------------

def test_level_1_matches_the_order_id(con):
    obligation(con, "order_A")
    m = matcher.match_settlement(con, captured(order_id="order_A"), NOW)
    assert m.level == 1 and m.obligation_id == "order_A"
    assert m.confidence == "certain" and m.certain is True


def test_level_1_prefers_our_notes_over_razorpays_link_order(con):
    """The Task-1 constraint paying off.

    The Create Payment Link API has no order_id parameter, so a payment made
    through our link carries an order id Razorpay minted for the link -- one the
    merchant has never seen. The real debt is in notes.obligation_id.
    """
    obligation(con, "order_REAL")
    payload = captured(order_id="order_PLINK_RAZORPAY_MADE_THIS",
                       notes=our_link_notes("order_REAL"))
    m = matcher.match_settlement(con, payload, NOW)
    assert m.level == 1 and m.obligation_id == "order_REAL"


def test_level_1_reads_the_reference_id(con):
    obligation(con, "order_REF")
    payload = {"event": "payment_link.paid", "id": "evt_ref",
               "payload": {"payment_link": {"entity": {
                   "id": "plink_1", "amount": 499_900, "status": "paid",
                   "reference_id": "order_REF#PAY_LINK"}}}}
    m = matcher.match_settlement(con, payload, NOW)
    assert m.level == 1 and m.obligation_id == "order_REF"


def test_level_1_does_not_invent_an_obligation(con):
    """An id we have never seen is not a match. It is an unmatched settlement."""
    m = matcher.match_settlement(con, captured(order_id="order_STRANGER"), NOW)
    assert m.matched is False and m.level is None


# -- level 2: customer id -------------------------------------------------

def test_level_2_customer_id_with_one_open_debt(con):
    obligation(con, "order_B", customer="cust_RZP1")
    m = matcher.match_settlement(con, captured(customer_id="cust_RZP1", amount=1), NOW)
    assert m.level == 2 and m.obligation_id == "order_B" and m.certain is True


def test_level_2_uses_the_amount_to_break_a_tie(con):
    obligation(con, "order_C1", customer="cust_RZP2", amount=100_000)
    obligation(con, "order_C2", customer="cust_RZP2", amount=750_000)
    m = matcher.match_settlement(con, captured(customer_id="cust_RZP2", amount=750_000), NOW)
    assert m.level == 2 and m.obligation_id == "order_C2"


def test_level_2_falls_through_when_it_cannot_tell(con):
    """Two debts of the same size for one customer: customer_id is not enough."""
    obligation(con, "order_D1", customer="cust_RZP3", amount=500_000)
    obligation(con, "order_D2", customer="cust_RZP3", amount=500_000)
    m = matcher.match_settlement(con, captured(customer_id="cust_RZP3", amount=500_000), NOW)
    assert m.level != 2                      # dropped to the amount rungs
    assert m.confidence == "ambiguous" and m.matched is False


# -- level 3: contact or email + amount + window --------------------------

def test_level_3_matches_on_phone_and_amount(con):
    obligation(con, "order_E", customer="cust9", contact="+919876543210")
    m = matcher.match_settlement(
        con, captured(contact="+919876543210", amount=499_900), NOW)
    assert m.level == 3 and m.obligation_id == "order_E"
    assert m.confidence == "strong" and m.certain is False   # strong is not certain


def test_level_3_matches_on_email(con):
    obligation(con, "order_F", customer="cust9", email="asha@example.com")
    m = matcher.match_settlement(
        con, captured(email="asha@example.com", amount=499_900), NOW)
    assert m.level == 3 and m.obligation_id == "order_F"


def test_level_3_tolerates_two_percent(con):
    obligation(con, "order_G", contact="+919876543210", amount=500_000)
    m = matcher.match_settlement(
        con, captured(contact="+919876543210", amount=495_000), NOW)   # -1%
    assert m.level == 3


def test_a_partial_payment_is_not_a_settlement(con):
    obligation(con, "order_H", contact="+919876543210", amount=500_000)
    m = matcher.match_settlement(
        con, captured(contact="+919876543210", amount=250_000), NOW)   # half
    assert m.matched is False


# -- level 4: the heuristic -----------------------------------------------

def test_level_4_is_labelled_a_heuristic(con):
    obligation(con, "order_I", amount=500_000)
    m = matcher.match_settlement(con, captured(amount=500_000), NOW)
    assert m.level == 4 and m.obligation_id == "order_I"
    assert m.confidence == "heuristic"
    assert m.certain is False                # THE point of the whole ladder
    assert "guess" in matcher.LADDER[4][2]   # and the UI copy says so too


def test_an_ambiguous_heuristic_closes_nothing(con):
    """Two debts within 2% of the amount. We cannot tell, so we do not guess.

    Closing the wrong case would book a recovery that did not happen AND keep
    chasing the customer who actually paid.
    """
    obligation(con, "order_J1", amount=500_000, customer="c1")
    obligation(con, "order_J2", amount=499_000, customer="c2")
    m = matcher.match_settlement(con, captured(amount=500_000), NOW)
    assert m.level == 4 and m.matched is False
    assert m.confidence == "ambiguous" and m.candidates == 2


def test_outside_the_window_is_not_a_match(con):
    obligation(con, "order_K", amount=500_000, opened=NOW - timedelta(days=30))
    m = matcher.match_settlement(con, captured(amount=500_000), NOW)
    assert m.matched is False


def test_a_settled_debt_is_not_matched_again_by_amount(con):
    obligation(con, "order_L", amount=500_000, status="SETTLED")
    m = matcher.match_settlement(con, captured(amount=500_000), NOW)
    assert m.matched is False


# -- the close path -------------------------------------------------------

def test_settlement_closes_the_case_and_cancels_work(con):
    from app.controllers import execute as ex
    from app.domain.models import ActionType

    case_id = obligation(con, "order_M")
    ex.schedule(con, case_id, "order_M", ActionType.PAY_LINK, NOW)
    store.save_payment_link(con, "order_M", "PAY_LINK", link_id="plink_1",
                            short_url="https://rzp.io/i/x", reference_id="order_M#PAY_LINK",
                            order_id="order_M", amount=499_900, status="created",
                            expires_at=None, created_at=NOW.isoformat(), dry_run=False)

    out = ing.ingest(con, captured(payment_id="pay_M", order_id="order_M"), now=NOW)
    assert out["action"] == "case_closed"
    assert out["cases_closed"] == 1 and out["actions_cancelled"] == 1
    assert out["links_closed"] == 1
    assert con.execute("SELECT status FROM actions WHERE obligation_id='order_M'"
                       ).fetchone()[0] == "CANCELLED"
    assert con.execute("SELECT status FROM payment_links WHERE obligation_id='order_M'"
                       ).fetchone()[0] == "paid"
    assert con.execute("SELECT status FROM obligations WHERE id='order_M'"
                       ).fetchone()[0] == "SETTLED"


def test_the_match_level_lands_on_the_case(con):
    obligation(con, "order_N", amount=500_000)
    ing.ingest(con, captured(payment_id="pay_N", amount=500_000), now=NOW)
    row = con.execute("SELECT match_level, match_basis, match_confidence FROM cases"
                      " WHERE obligation_id='order_N'").fetchone()
    assert row["match_level"] == 4 and row["match_confidence"] == "heuristic"


def test_a_link_we_sent_makes_the_recovery_ours(con):
    obligation(con, "order_O")
    out = ing.ingest(con, captured(payment_id="pay_O", order_id="order_O",
                                   notes=our_link_notes("order_O")), now=NOW)
    assert out["status"] == "RECOVERED"
    assert out["attributed"] is True
    assert "link we sent" in out["attribution_reason"]


def test_a_contact_we_made_makes_the_recovery_ours(con):
    case_id = obligation(con, "order_P", customer="cust_P")
    store.record_contact(con, customer_id="cust_P", obligation_id="order_P",
                         case_id=case_id, channel="email", action="PAY_LINK",
                         tier="static", used_llm=False, subject="s",
                         sent_at=NOW.isoformat(), ok=True, detail="sent")
    out = ing.ingest(con, captured(payment_id="pay_P", order_id="order_P"), now=NOW)
    assert out["status"] == "RECOVERED" and out["attributed"] is True


def test_a_failed_contact_does_not_make_the_recovery_ours(con):
    """We could not reach them and they paid anyway. That is not our recovery."""
    case_id = obligation(con, "order_Q", customer="cust_Q")
    store.record_contact(con, customer_id="cust_Q", obligation_id="order_Q",
                         case_id=case_id, channel="email", action="PAY_LINK",
                         tier="static", used_llm=False, subject="s",
                         sent_at=NOW.isoformat(), ok=False, detail="SMTP_FAILED")
    out = ing.ingest(con, captured(payment_id="pay_Q", order_id="order_Q"), now=NOW)
    assert out["status"] == "SELF_RECOVERED" and out["attributed"] is False


def test_no_contact_is_self_recovered(con):
    obligation(con, "order_R")
    out = ing.ingest(con, captured(payment_id="pay_R", order_id="order_R"), now=NOW)
    assert out["status"] == "SELF_RECOVERED"


def test_an_unmatched_settlement_closes_nothing_but_is_recorded(con):
    obligation(con, "order_S1", amount=500_000, customer="c1")
    obligation(con, "order_S2", amount=500_000, customer="c2")
    out = ing.ingest(con, captured(payment_id="pay_S", amount=500_000), now=NOW)
    assert out["action"] == "settlement_unmatched"
    assert con.execute("SELECT COUNT(*) FROM cases WHERE status='OPEN'").fetchone()[0] == 2
    assert len(store.unmatched_settlements(con)) == 1


# -- one payment, one recovery -------------------------------------------

def test_two_events_for_one_payment_count_once(con):
    """`payment.captured` and `order.paid` describe the same money."""
    obligation(con, "order_T")
    first = ing.ingest(con, captured(payment_id="pay_T", order_id="order_T"), now=NOW)
    second = ing.ingest(con, captured(payment_id="pay_T", order_id="order_T",
                                      event="order.paid"), now=NOW)
    assert first["counted"] is True and second["counted"] is False
    assert con.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1
    d = store.match_distribution(con)
    assert d["settlements"] == 1 and d["amount"] == 499_900


# -- level 5: the ledger hook --------------------------------------------

def test_level_5_ledger_hook_closes_the_case_as_asserted(con):
    case_id = obligation(con, "order_U", amount=750_000)
    out = ing.settle_from_ledger(con, case_id, NOW, who="ops@merchant",
                                 reference="NEFT/998877", note="bank transfer")
    assert out["ok"] is True and out["cases_closed"] == 1
    assert out["match"]["level"] == 5
    assert out["match"]["confidence"] == "asserted"
    assert out["match"]["certain"] is False       # we did not observe this money
    assert "998877" in out["match"]["evidence"]
    assert con.execute("SELECT status FROM cases WHERE case_id=?",
                       (case_id,)).fetchone()[0] == "RECOVERED"


def test_the_ledger_hook_is_idempotent(con):
    case_id = obligation(con, "order_V")
    first = ing.settle_from_ledger(con, case_id, NOW, who="ops")
    con.execute("UPDATE cases SET status='OPEN' WHERE case_id=?", (case_id,))
    con.commit()
    second = ing.settle_from_ledger(con, case_id, NOW, who="ops")
    assert first["counted"] is True and second["counted"] is False


def test_the_ledger_hook_rejects_an_unknown_case(con):
    out = ing.settle_from_ledger(con, "live_nope", NOW)
    assert out["ok"] is False


# -- the distribution the dashboard shows --------------------------------

def test_distribution_separates_certain_from_the_rest(con):
    obligation(con, "order_W1")                                  # level 1
    obligation(con, "order_W2", amount=333_000)                  # level 4
    ing.ingest(con, captured(payment_id="p1", order_id="order_W1"), now=NOW)
    ing.ingest(con, captured(payment_id="p2", amount=333_000), now=NOW)

    d = store.match_distribution(con)
    assert d["settlements"] == 2
    assert d["amount"] == 499_900 + 333_000
    assert d["amount_certain"] == 499_900
    assert d["amount_not_certain"] == 333_000
    assert 0 < d["pct_certain"] < 100
    levels = {r["level"]: r for r in d["levels"]}
    assert levels[1]["confidence"] == "certain"
    assert levels[4]["confidence"] == "heuristic"


def test_every_ladder_rung_has_ui_copy():
    for level, (basis, confidence, means) in matcher.LADDER.items():
        assert basis and means
        assert confidence in ("certain", "strong", "heuristic", "asserted")
    assert matcher.CERTAIN_LEVELS == {1, 2}


# -- the fixtures on disk -------------------------------------------------

def test_the_our_link_fixture_settles_at_level_one(con):
    """End to end from a real-shaped payload, with no tunnel."""
    payload = json.loads((FIXTURES / "06_payment_captured_via_our_link.json").read_text())
    case_id = obligation(con, "order_TEST000000006", customer="cust_TEST00000006")
    out = ing.ingest(con, payload, now=datetime(2026, 2, 26, 12, 0))

    assert out["match"]["level"] == 1
    assert out["obligation_id"] == "order_TEST000000006"    # not Razorpay's link order
    assert out["status"] == "RECOVERED"
    assert con.execute("SELECT status FROM cases WHERE case_id=?",
                       (case_id,)).fetchone()[0] == "RECOVERED"


def test_the_payment_link_paid_fixture_settles_at_level_one(con):
    payload = json.loads((FIXTURES / "07_payment_link_paid.json").read_text())
    obligation(con, "order_TEST000000007", customer="cust_TEST00000007")
    out = ing.ingest(con, payload, now=datetime(2026, 2, 26, 12, 0))
    assert out["match"]["level"] == 1 and out["status"] == "RECOVERED"


def test_an_unmatched_settlement_is_also_counted_once(con):
    """Razorpay re-delivers until it gets a 200, and an unmatched settlement is the
    row an operator has to reconcile by hand. Two rows for one payment would send
    them looking for money that arrived once."""
    payload = captured(payment_id="pay_ORPHAN", amount=777_000)
    first = ing.ingest(con, payload, now=NOW)
    second = ing.ingest(con, {**payload, "id": "evt_orphan_again"}, now=NOW)
    assert first["action"] == "settlement_unmatched"
    assert first["counted"] is True and second["counted"] is False
    assert len(store.unmatched_settlements(con)) == 1
