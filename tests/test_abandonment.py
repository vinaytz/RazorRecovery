"""
Abandoned checkout: the at-risk source that arrives as an absence.

There is no `checkout.abandoned` webhook, so this is the one source that cannot be
event-driven. What is being pinned here:

  the absence is detected     an order with no payment becomes a case, once
  the presence is not         an order 30 seconds old is NOT at risk
  the domain did not change   same snapshot, same gates, same engine, same executor

That last one is the real assertion. A new revenue source should need new
plumbing, not a new core -- `test_the_domain_layer_did_not_learn_about_checkouts`
reads the source of app/domain/ and fails if this feature leaked into it.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.controllers import ingest as ing
from app.repos import store
from app.workers import sweeper

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "webhooks"
NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


def order_created(*, order_id="order_AB1", amount=289_900, customer_id="cust_AB1",
                  notes=None, event_id=None):
    ent = {"id": order_id, "entity": "order", "amount": amount, "amount_paid": 0,
           "amount_due": amount, "currency": "INR", "status": "created",
           "attempts": 0, "customer_id": customer_id}
    if notes is not None:
        ent["notes"] = notes
    return {"event": "order.created", "id": event_id or f"evt_{order_id}_created",
            "payload": {"order": {"entity": ent}}}


def captured(*, order_id="order_AB1", amount=289_900, payment_id="pay_AB1"):
    return {"event": "payment.captured", "id": f"evt_{payment_id}",
            "payload": {"payment": {"entity": {
                "id": payment_id, "amount": amount, "status": "captured",
                "method": "upi", "captured": True, "order_id": order_id}}}}


# -- an order is a clock, not a case --------------------------------------

def test_order_created_opens_no_case(con):
    """The most important negative in the file. A customer on the payment page is
    not a recovery target."""
    out = ing.ingest(con, order_created(), now=NOW)
    assert out["action"] == "checkout_watched"
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 0
    assert con.execute("SELECT status FROM checkouts WHERE order_id='order_AB1'"
                       ).fetchone()[0] == "WATCHING"


def test_a_fresh_order_is_not_swept(con):
    ing.ingest(con, order_created(), now=NOW)
    out = sweeper.sweep(con, NOW + timedelta(minutes=29), minutes=30)
    assert out["opened"] == []
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_the_window_closing_opens_a_case(con):
    ing.ingest(con, order_created(), now=NOW)
    out = sweeper.sweep(con, NOW + timedelta(minutes=31), minutes=30)
    assert len(out["opened"]) == 1
    case = con.execute("SELECT * FROM cases WHERE case_id='live_order_AB1'").fetchone()
    assert case["failure_class"] == "CHECKOUT_ABANDONED"
    assert case["kind"] == "CHECKOUT"
    assert case["arm"] == "ENGINE" and case["status"] == "OPEN"
    assert case["amount"] == 289_900
    assert case["attempts"] == 0 and case["rung"] == 0   # nobody tried and failed
    assert con.execute("SELECT status FROM checkouts WHERE order_id='order_AB1'"
                       ).fetchone()[0] == "ABANDONED"


def test_a_paid_order_never_becomes_a_case(con):
    """The failure that would embarrass us: emailing somebody who already paid."""
    ing.ingest(con, order_created(), now=NOW)
    ing.ingest(con, captured(), now=NOW + timedelta(minutes=2))
    out = sweeper.sweep(con, NOW + timedelta(hours=3), minutes=30)
    assert out["opened"] == []
    assert con.execute("SELECT status FROM checkouts WHERE order_id='order_AB1'"
                       ).fetchone()[0] == "PAID"


def test_a_payment_through_our_link_clears_the_watch_list(con):
    """A link payment carries Razorpay's link order id AND ours in notes. Both are
    cleared, or the debt we just settled gets swept as an abandoned checkout."""
    ing.ingest(con, order_created(order_id="order_LINKED"), now=NOW)
    payload = captured(order_id="order_PLINK_RZP_MADE_THIS", payment_id="pay_LINKED")
    payload["payload"]["payment"]["entity"]["notes"] = {
        "obligation_id": "order_LINKED", "order_id": "order_LINKED",
        "recovery_action": "PAY_LINK", "source": "razorrecovery"}
    ing.ingest(con, payload, now=NOW + timedelta(minutes=5))
    assert con.execute("SELECT status FROM checkouts WHERE order_id='order_LINKED'"
                       ).fetchone()[0] == "PAID"
    assert sweeper.sweep(con, NOW + timedelta(hours=3), minutes=30)["opened"] == []


def test_an_unmatched_settlement_still_clears_its_own_order(con):
    """We could not tell which DEBT it closed, but the ids prove THIS order was
    paid. Sweeping it anyway would chase a customer holding a receipt."""
    ing.ingest(con, order_created(order_id="order_ORPHAN"), now=NOW)
    con.execute("DELETE FROM obligations")     # no debt on file to match against
    con.commit()
    out = ing.ingest(con, captured(order_id="order_ORPHAN", payment_id="pay_ORPHAN"),
                     now=NOW + timedelta(minutes=1))
    assert out["action"] == "settlement_unmatched"
    assert con.execute("SELECT status FROM checkouts WHERE order_id='order_ORPHAN'"
                       ).fetchone()[0] == "PAID"


def test_a_settlement_we_never_saw_is_caught_at_sweep_time(con):
    """Belt and braces: the obligation is SETTLED (a level-4 match, or an operator's
    ledger entry) but the watch row was never cleared. The sweeper re-checks."""
    ing.ingest(con, order_created(order_id="order_LATE"), now=NOW)
    con.execute("INSERT OR REPLACE INTO obligations (id, customer_id, amount_due,"
                " amount_settled, status, opened_at) VALUES (?,?,?,?,?,?)",
                ("order_LATE", "c", 289_900, 289_900, "SETTLED", NOW.isoformat()))
    con.commit()
    out = sweeper.sweep(con, NOW + timedelta(hours=2), minutes=30)
    assert out["opened"] == [] and len(out["skipped"]) == 1
    assert out["skipped"][0]["why"] == "already settled"


# -- idempotency ----------------------------------------------------------

def test_the_clock_starts_when_the_order_was_created_not_when_we_heard(con):
    """The window measures the CUSTOMER's silence, so it starts at the order, not at
    the delivery. A webhook held up 40 minutes by an outage must not buy a fresh 30
    minutes of grace -- that makes us slowest exactly when we are already late."""
    payload = order_created()
    payload["payload"]["order"]["entity"]["created_at"] = int(
        (NOW - timedelta(minutes=40)).timestamp())

    ing.ingest(con, payload, now=NOW)          # we hear about it 40 minutes late
    row = con.execute("SELECT created_at, seen_at FROM checkouts").fetchone()
    assert row["created_at"] < row["seen_at"]

    out = sweeper.sweep(con, NOW, minutes=30)  # already abandoned on arrival
    assert len(out["opened"]) == 1


def test_a_missing_timestamp_waits_rather_than_chases(con):
    """No usable created_at: start from now. Wrong in the safe direction."""
    payload = order_created()
    payload["payload"]["order"]["entity"].pop("created_at", None)
    ing.ingest(con, payload, now=NOW)
    assert sweeper.sweep(con, NOW + timedelta(minutes=29), minutes=30)["opened"] == []
    assert len(sweeper.sweep(con, NOW + timedelta(minutes=31), minutes=30)["opened"]) == 1


def test_a_redelivered_order_created_does_not_reset_the_clock(con):
    """Razorpay retries. If a retry reset `created_at`, an order could be pushed out
    of the abandonment window forever by delivery attempts alone."""
    ing.ingest(con, order_created(), now=NOW)
    again = ing.ingest(con, order_created(event_id="evt_different_delivery"),
                       now=NOW + timedelta(minutes=25))
    assert again["action"] == "checkout_already_watched"
    row = con.execute("SELECT created_at FROM checkouts WHERE order_id='order_AB1'"
                      ).fetchone()
    assert row["created_at"] == NOW.isoformat()
    assert len(sweeper.sweep(con, NOW + timedelta(minutes=31), minutes=30)["opened"]) == 1


def test_sweeping_twice_opens_one_case(con):
    ing.ingest(con, order_created(), now=NOW)
    later = NOW + timedelta(hours=1)
    first = sweeper.sweep(con, later, minutes=30)
    second = sweeper.sweep(con, later, minutes=30)
    assert len(first["opened"]) == 1 and second["opened"] == []
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1


def test_a_later_payment_failure_updates_the_case_it_does_not_duplicate_it(con):
    """The sweeper mints the same `live_{order_id}` case id ingest would, so a
    failure arriving afterwards lands on the same case."""
    ing.ingest(con, order_created(), now=NOW)
    sweeper.sweep(con, NOW + timedelta(minutes=31), minutes=30)
    out = ing.ingest(con, {"event": "payment.failed", "id": "evt_late_fail",
                           "payload": {"payment": {"entity": {
                               "id": "pay_late", "order_id": "order_AB1",
                               "amount": 289_900, "method": "card",
                               "error_reason": "card_expired"}}}},
                     now=NOW + timedelta(minutes=40))
    assert out["action"] == "case_updated"
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1


# -- reachability ---------------------------------------------------------

def test_contact_details_come_off_the_order_notes(con):
    """An order has no payer on it -- there is no payment yet. What reachability
    exists is in notes, and without it the sweeper opens an unemailable case."""
    ing.ingest(con, order_created(notes={"name": "Rohit Sharma",
                                        "contact": "+919000000008",
                                        "email": "rohit@example.com"}), now=NOW)
    sweeper.sweep(con, NOW + timedelta(hours=1), minutes=30)
    ob = con.execute("SELECT * FROM obligations WHERE id='order_AB1'").fetchone()
    assert ob["email"] == "rohit@example.com"
    assert ob["contact"] == "+919000000008"
    assert ob["name"] == "Rohit Sharma"


# -- the window is configurable, and safely -------------------------------

def test_the_window_defaults_to_thirty_minutes(monkeypatch):
    monkeypatch.delenv("ABANDON_MINUTES", raising=False)
    assert sweeper.abandon_minutes() == 30


@pytest.mark.parametrize("raw", ["", "0", "-5", "soon", "1.5"])
def test_a_nonsense_window_falls_back_to_the_default(monkeypatch, raw):
    """A zero or negative window would sweep every order the instant it was
    created. Bad config must not become an outbound email."""
    monkeypatch.setenv("ABANDON_MINUTES", raw)
    assert sweeper.abandon_minutes() == 30


def test_the_env_shortens_the_window_for_the_demo(monkeypatch, con):
    monkeypatch.setenv("ABANDON_MINUTES", "1")
    ing.ingest(con, order_created(), now=NOW)
    assert len(sweeper.sweep(con, NOW + timedelta(minutes=2))["opened"]) == 1


# -- the case is indistinguishable downstream ----------------------------

def test_an_abandoned_case_decides_like_any_other(con):
    """The point of the whole item: the engine cannot tell how the case was born.

    Built through the same `CaseSnapshot` the benchmark uses, run through the real
    gates and the real `decide`, with nothing special-cased for checkouts.
    """
    import numpy as np

    from app.config_loader import load_config
    from app.domain.engine import decide
    from app.domain.models import Arm, CaseSnapshot, FailureClass, ObligationKind
    from app.services.bandit import Posterior

    ing.ingest(con, order_created(), now=NOW)
    sweeper.sweep(con, NOW + timedelta(minutes=31), minutes=30)
    row = con.execute("SELECT * FROM cases WHERE case_id='live_order_AB1'").fetchone()

    cfg = load_config()
    snap = CaseSnapshot(
        case_id=row["case_id"], obligation_id=row["obligation_id"],
        customer_id=row["customer_id"], merchant_id="m1", arm=Arm.ENGINE,
        amount_due=row["amount"], amount_settled=0,
        kind=ObligationKind(row["kind"]), currency="INR",
        failure_class=FailureClass(row["failure_class"]), method="upi",
        is_mandate=False, now=NOW + timedelta(minutes=31),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        attempts=row["attempts"], rung=row["rung"], last_action_at=None,
        promised_until=None, customer_tenure_days=200, customer_past_failures=0,
        customer_past_recoveries=1, contacts_last_7d=0, opted_out=False,
        risk_blocked=False, last_notice_sent_at=None, afa_valid=True,
        obligation_settled=False, method_in_downtime=False, downtime_ends_at=None)

    d = decide(snap, cfg, Posterior(cfg), np.random.default_rng(0))
    assert d.action is not None
    assert [g.gate for g in d.gate_trace][0].startswith("G0")   # holdout still first


def test_the_domain_layer_did_not_learn_about_checkouts():
    """ZERO changes to app/domain/ for this feature.

    CHECKOUT_ABANDONED and ObligationKind.CHECKOUT were already in the enums before
    this item; what must not appear is any awareness of the watch list, the sweeper,
    or the window. If one of these strings shows up in the pure core, a live-path
    concern has leaked into the decision core.
    """
    src = "\n".join(p.read_text() for p in sorted((ROOT / "app" / "domain").glob("*.py")))
    for leak in ("checkout_watched", "checkouts", "sweeper", "sweep",
                 "ABANDON_MINUTES", "abandon_minutes", "WATCHING"):
        assert leak not in src, f"'{leak}' leaked into app/domain/"


def test_git_says_the_domain_layer_is_untouched():
    """The claim above, checked against the actual diff rather than a grep.

    Pinned to item 1c's own commit, not to HEAD. Against HEAD this asserted only
    that the working tree had no uncommitted domain changes, which any later commit
    makes vacuously true -- and item 3a, which does legitimately change a gate, is
    what exposed that. The claim being made is about what the ABANDONMENT work
    touched, so it is that commit's diff that has to be empty, forever.

    Skips rather than fails when git cannot answer -- a tarball checkout is not a
    test failure -- but on a real clone this is the assertion that counts.
    """
    ABANDONMENT_COMMIT = "366894b"        # "1c: abandoned checkouts"
    try:
        out = subprocess.run(
            ["git", "diff", "--stat", f"{ABANDONMENT_COMMIT}^", ABANDONMENT_COMMIT,
             "--", "app/domain/"],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):    # pragma: no cover
        pytest.skip("git unavailable")
    if out.returncode != 0:                          # pragma: no cover
        pytest.skip("not a git checkout, or the commit is not in this history")
    assert out.stdout.strip() == "", f"app/domain/ was modified by 1c:\n{out.stdout}"


# -- the fixture on disk --------------------------------------------------

def test_the_fixture_goes_from_order_to_case(con):
    payload = json.loads((FIXTURES / "08_order_created_then_abandoned.json").read_text())
    # From the fixture's own epoch, not a hardcoded wall clock: the window is measured
    # off `order.created_at`, and `fromtimestamp` resolves it in the local zone.
    created = datetime.fromtimestamp(payload["payload"]["order"]["entity"]["created_at"])

    out = ing.ingest(con, payload, now=created)
    assert out["action"] == "checkout_watched"
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0

    swept = sweeper.sweep(con, created + timedelta(minutes=31), minutes=30)
    assert swept["opened"][0]["case_id"] == "live_order_TEST000000008"
    case = con.execute("SELECT * FROM cases WHERE case_id='live_order_TEST000000008'"
                       ).fetchone()
    assert case["failure_class"] == "CHECKOUT_ABANDONED" and case["amount"] == 289_900
    ob = con.execute("SELECT * FROM obligations WHERE id='order_TEST000000008'").fetchone()
    assert ob["email"] == "rohit@example.com"     # reachable, from the order notes
