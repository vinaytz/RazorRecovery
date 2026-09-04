"""
When to act. For bank-side failures, *when* beats *what you say*.

Pure: `now` arrives on the snapshot, never from a clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import (
    ActionType,
    CaseSnapshot,
    Config,
    FailureClass,
    GateResult,
)
from app.domain.gates import in_quiet_hours, next_contact_window

# Rung-appropriate delays. Retry fast, escalate slowly.
BASE_DELAY_HOURS: dict[ActionType, float] = {
    ActionType.WAIT: 1.0,
    ActionType.RETRY: 0.0,
    ActionType.REMIND: 2.0,
    ActionType.PAY_LINK: 6.0,
    ActionType.METHOD_CHANGE: 12.0,
    ActionType.HUMAN: 24.0,
}


def when(snap: CaseSnapshot, action: ActionType, gates: GateResult, cfg: Config) -> datetime:
    t = snap.now + timedelta(hours=BASE_DELAY_HOURS.get(action, 1.0))

    # A bank outage does not care about our schedule.
    if action == ActionType.RETRY and snap.method_in_downtime:
        resume = snap.downtime_ends_at or (snap.now + timedelta(minutes=cfg.downtime_backoff_minutes))
        t = max(t, resume)

    # No money in the account today does not mean no money on payday.
    if (action == ActionType.RETRY
            and snap.failure_class == FailureClass.INSUFFICIENT_FUNDS
            and cfg.insufficient_funds_wait_for_payday
            and t.day not in cfg.payday_window_days):
        t = _next_payday(t, cfg)

    # A mandate debit needs its 24h notice served first.
    if action == ActionType.RETRY and snap.is_mandate:
        earliest = (snap.last_notice_sent_at or snap.now) + timedelta(
            hours=cfg.mandate_pre_debit_notice_hours)
        t = max(t, earliest)

    # Never wake a customer up.
    if action in (ActionType.REMIND, ActionType.PAY_LINK, ActionType.METHOD_CHANGE, ActionType.HUMAN):
        if in_quiet_hours(t, cfg.quiet_start, cfg.quiet_end):
            t = next_contact_window(t, cfg.quiet_start, cfg.quiet_end)

    if gates.wait_until and t < gates.wait_until:
        t = gates.wait_until

    return t


def _next_payday(t: datetime, cfg: Config) -> datetime:
    for _ in range(40):
        t = (t + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        if t.day in cfg.payday_window_days:
            return t
    return t
