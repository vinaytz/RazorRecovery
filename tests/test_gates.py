"""The assertions worth losing sleep over."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.config_loader import load_config
from app.domain.gates import in_quiet_hours, run_gates
from app.domain.ladder import legal_next_rungs
from app.domain.models import (
    ActionType, Arm, CaseSnapshot, FailureClass, ObligationKind, StopReason,
)

CFG = load_config()
T0 = datetime(2026, 3, 10, 14, 0, 0)      # a Tuesday afternoon: outside quiet hours


def _detail(g, gate: str) -> str:
    """The trace line for one gate, so a test can assert on the reason it gave."""
    return next((t.detail for t in g.trace if t.gate == gate), "")


def snap(**kw) -> CaseSnapshot:
    base = dict(
        case_id="c1", obligation_id="order_1", customer_id="cust_1",
        merchant_id="m1", arm=Arm.ENGINE,
        amount_due=500_000, amount_settled=0, kind=ObligationKind.ORDER, currency="INR",
        failure_class=FailureClass.INSUFFICIENT_FUNDS, method="card", is_mandate=False,
        now=T0, opened_at=T0 - timedelta(hours=2), attempts=0, rung=0,
        last_action_at=None, promised_until=None,
        customer_tenure_days=100, customer_past_failures=1, customer_past_recoveries=0,
        contacts_last_7d=0, opted_out=False, risk_blocked=False,
        last_notice_sent_at=None, afa_valid=True,
        obligation_settled=False, method_in_downtime=False, downtime_ends_at=None,
        pending_action_types=(),
    )
    base.update(kw)
    return CaseSnapshot(**base)


def test_control_arm_can_never_act():
    """If this ever fails, the headline number is fiction."""
    g = run_gates(snap(arm=Arm.CONTROL), CFG)
    assert g.blocked and g.stop_reason == StopReason.CONTROL_ARM
    assert g.trace[0].gate == "G0_HOLDOUT"          # first gate, always


def test_settled_obligation_is_never_chased():
    g = run_gates(snap(obligation_settled=True), CFG)
    assert g.blocked and g.stop_reason == StopReason.ALREADY_SETTLED


def test_expired_card_blocks_retry_but_not_method_change():
    g = run_gates(snap(failure_class=FailureClass.CARD_EXPIRED), CFG)
    assert not g.blocked
    assert ActionType.RETRY in g.blocked_actions
    assert ActionType.METHOD_CHANGE not in g.blocked_actions


def test_no_mandate_blocks_retry_and_the_ladder_starts_at_a_real_lever():
    """Item 3a. A one-time order holds no instrument, so RETRY cannot reach money.

    Before this gate the engine spent rung 1 on a RETRY the executor reported back
    as INTENT_ONLY, having contacted nobody -- the before-shot is in
    docs/evidence/pre_3a_wasted_rung.txt. The rung is the scarce thing here, not
    the API call.
    """
    g = run_gates(snap(is_mandate=False, kind=ObligationKind.ORDER), CFG)
    assert not g.blocked                       # the case goes on, it just cannot retry
    assert ActionType.RETRY in g.blocked_actions
    assert ActionType.PAY_LINK not in g.blocked_actions
    assert StopReason.NO_MANDATE_TO_RETRY.value in _detail(g, "G8_NON_RETRYABLE")

    # and the ladder climbs past the dead rung rather than stalling on it
    first = [a for a in legal_next_rungs(snap(), g, CFG) if a != ActionType.WAIT]
    assert ActionType.RETRY not in first


def test_a_mandate_may_still_retry():
    """The other half. If 3a blocked every retry it would not be a gate, it would
    be a deletion of the rung."""
    g = run_gates(snap(is_mandate=True, kind=ObligationKind.SUBSCRIPTION,
                       last_notice_sent_at=T0 - timedelta(hours=48)), CFG)
    assert ActionType.RETRY not in g.blocked_actions


def test_no_mandate_covers_checkout_and_invoice_not_just_orders():
    """The gate asks about the mandate, not the kind.

    `app/workers/sweeper.py` opens abandoned checkouts as kind=CHECKOUT, and it was
    a checkout that had never held an instrument at all. A gate keyed on
    kind == ORDER would have left exactly that case retrying.
    """
    for kind in (ObligationKind.CHECKOUT, ObligationKind.INVOICE, ObligationKind.ORDER):
        g = run_gates(snap(is_mandate=False, kind=kind), CFG)
        assert ActionType.RETRY in g.blocked_actions, kind


def test_mandate_without_pre_debit_notice_blocks_retry():
    g = run_gates(snap(is_mandate=True, last_notice_sent_at=None), CFG)
    assert ActionType.RETRY in g.blocked_actions


def test_mandate_above_afa_limit_blocks_retry():
    g = run_gates(snap(is_mandate=True, amount_due=2_000_000, afa_valid=False,
                       last_notice_sent_at=T0 - timedelta(hours=48)), CFG)
    assert ActionType.RETRY in g.blocked_actions


def test_quiet_hours_block_contact_only():
    late = T0.replace(hour=23)
    # A mandate with its notice already served, because the point of this test is
    # that quiet hours touch CONTACT actions and leave a debit alone. On a snapshot
    # with no mandate, G8 blocks RETRY for its own reason and the assertion below
    # would pass without quiet hours having anything to do with it.
    g = run_gates(snap(now=late, is_mandate=True,
                       last_notice_sent_at=late - timedelta(hours=48)), CFG)
    assert ActionType.REMIND in g.blocked_actions
    assert ActionType.RETRY not in g.blocked_actions
    assert g.wait_until is not None and g.wait_until.hour == CFG.quiet_end


def test_expired_window_writes_off():
    old = snap(opened_at=T0 - timedelta(hours=CFG.window_hours + 1))
    g = run_gates(old, CFG)
    assert g.blocked and g.terminal_action == ActionType.WRITE_OFF


def test_quiet_hours_wraps_midnight():
    assert in_quiet_hours(T0.replace(hour=22), 21, 9)
    assert in_quiet_hours(T0.replace(hour=3), 21, 9)
    assert not in_quiet_hours(T0.replace(hour=14), 21, 9)
