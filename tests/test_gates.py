"""The assertions worth losing sleep over."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.config_loader import load_config
from app.domain.gates import in_quiet_hours, run_gates
from app.domain.models import (
    ActionType, Arm, CaseSnapshot, FailureClass, ObligationKind, StopReason,
)

CFG = load_config()
T0 = datetime(2026, 3, 10, 14, 0, 0)      # a Tuesday afternoon: outside quiet hours


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


def test_mandate_without_pre_debit_notice_blocks_retry():
    g = run_gates(snap(is_mandate=True, last_notice_sent_at=None), CFG)
    assert ActionType.RETRY in g.blocked_actions


def test_mandate_above_afa_limit_blocks_retry():
    g = run_gates(snap(is_mandate=True, amount_due=2_000_000, afa_valid=False,
                       last_notice_sent_at=T0 - timedelta(hours=48)), CFG)
    assert ActionType.RETRY in g.blocked_actions


def test_quiet_hours_block_contact_only():
    late = T0.replace(hour=23)
    g = run_gates(snap(now=late), CFG)
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
