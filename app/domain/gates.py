"""
The hard gate layer. Ordered. First blocking match wins.

G0 (holdout) runs FIRST, always. That ordering is the single thing that makes the
control arm honest, and therefore the single thing the headline number rests on.
If a CONTROL case ever produces an action, this file is wrong.

Gates are the LLM-free part of the system. Nothing here is probabilistic, nothing
here is learned, and nothing downstream may override a block.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import (
    CONTACT_ACTIONS,
    NON_RETRYABLE_FAILURES,
    ActionType,
    Arm,
    CaseSnapshot,
    Config,
    GateResult,
    GateTrace,
    StopReason,
)


def in_quiet_hours(now: datetime, start_h: int, end_h: int) -> bool:
    """Quiet hours wrap midnight: start=21, end=9 -> blocked 21:00..08:59."""
    h = now.hour
    if start_h == end_h:
        return False
    if start_h > end_h:            # wraps midnight
        return h >= start_h or h < end_h
    return start_h <= h < end_h


def next_contact_window(now: datetime, start_h: int, end_h: int) -> datetime:
    """First moment contact becomes legal again."""
    if not in_quiet_hours(now, start_h, end_h):
        return now
    candidate = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def run_gates(snap: CaseSnapshot, cfg: Config) -> GateResult:
    trace: list[GateTrace] = []
    blocked_actions: set[ActionType] = set()
    wait_until: datetime | None = None
    wait_reason: StopReason | None = None

    def stop(gate: str, reason: StopReason, detail: str = "",
             terminal: ActionType | None = None) -> GateResult:
        trace.append(GateTrace(gate, passed=False, detail=detail or reason.value))
        return GateResult(
            blocked=True, stop_reason=reason, terminal_action=terminal,
            blocked_actions=frozenset(blocked_actions), trace=tuple(trace),
        )

    def ok(gate: str, detail: str = "") -> None:
        trace.append(GateTrace(gate, passed=True, detail=detail))

    # -- G0 HOLDOUT --------------------------------------------------------
    # The control arm never acts. Ever. This is the billing meter and the
    # experiment's control group at the same time.
    if snap.arm == Arm.CONTROL:
        return stop("G0_HOLDOUT", StopReason.CONTROL_ARM, "control arm: never act")
    ok("G0_HOLDOUT", f"arm={snap.arm.value}")

    # -- G1 ALREADY SETTLED ------------------------------------------------
    # The money may have arrived without us: customer retried, paid by another
    # route, or a late capture landed. Chasing a paying customer is the worst
    # failure this system can have.
    if snap.obligation_settled or snap.amount_remaining <= 0:
        return stop("G1_SETTLED", StopReason.ALREADY_SETTLED, "obligation already settled")
    ok("G1_SETTLED", f"remaining={snap.amount_remaining}")

    # -- G2 PROMISED TO PAY ------------------------------------------------
    if snap.promised_until and snap.now < snap.promised_until:
        trace.append(GateTrace("G2_PROMISED", False, f"promised until {snap.promised_until.isoformat()}"))
        return GateResult(
            blocked=False, blocked_actions=frozenset(ActionType),
            wait_until=snap.promised_until, wait_reason=StopReason.PROMISED,
            trace=tuple(trace),
        )
    ok("G2_PROMISED")

    # -- G3 RISK -----------------------------------------------------------
    if snap.risk_blocked:
        return stop("G3_RISK", StopReason.RISK_BLOCK, "external risk block")
    ok("G3_RISK")

    # -- G4 OPTED OUT ------------------------------------------------------
    if snap.opted_out:
        return stop("G4_OPTED_OUT", StopReason.OPTED_OUT, "customer opted out")
    ok("G4_OPTED_OUT")

    # -- G5 RECOVERY WINDOW ------------------------------------------------
    if snap.age_hours > cfg.window_hours:
        return stop("G5_WINDOW", StopReason.WINDOW_EXPIRED,
                    f"age {snap.age_hours:.1f}h > {cfg.window_hours}h",
                    terminal=ActionType.WRITE_OFF)
    ok("G5_WINDOW", f"age={snap.age_hours:.1f}h")

    # -- G6 RETRY BUDGET ---------------------------------------------------
    if snap.attempts >= cfg.max_attempts:
        blocked_actions.add(ActionType.RETRY)
        trace.append(GateTrace("G6_RETRY_BUDGET", False,
                               f"attempts={snap.attempts} >= {cfg.max_attempts}"))
    else:
        ok("G6_RETRY_BUDGET", f"attempts={snap.attempts}")

    # -- G7 LADDER TOP -----------------------------------------------------
    if snap.rung >= cfg.max_rung:
        return stop("G7_LADDER_TOP", StopReason.LADDER_TOP,
                    f"rung {snap.rung} at cap {cfg.max_rung}",
                    terminal=ActionType.WRITE_OFF)
    ok("G7_LADDER_TOP", f"rung={snap.rung}")

    # -- G8 NON-RETRYABLE INSTRUMENT ---------------------------------------
    # An expired card does not become unexpired because you asked twice.
    if snap.failure_class in NON_RETRYABLE_FAILURES:
        blocked_actions.add(ActionType.RETRY)
        trace.append(GateTrace("G8_NON_RETRYABLE", False,
                               f"{snap.failure_class.value}: retry pointless, method change legal"))
    else:
        ok("G8_NON_RETRYABLE")

    # -- G9 DOWNTIME -------------------------------------------------------
    # Razorpay tells us the issuer is down. Retrying into a known outage is
    # setting the retry budget on fire.
    if snap.method_in_downtime:
        blocked_actions.add(ActionType.RETRY)
        until = snap.downtime_ends_at or (snap.now + timedelta(minutes=cfg.downtime_backoff_minutes))
        if wait_until is None or until > wait_until:
            wait_until, wait_reason = until, StopReason.DOWNTIME
        trace.append(GateTrace("G9_DOWNTIME", False, f"{snap.method} down until {until.isoformat()}"))
    else:
        ok("G9_DOWNTIME")

    # -- G10 MANDATE PRE-DEBIT NOTICE --------------------------------------
    # RBI e-mandate framework: the customer gets at least 24h notice before a
    # debit, with a chance to opt out. Retrying a mandate without it is not a
    # product decision, it is a compliance breach.
    if snap.is_mandate:
        notice_ok = (
            snap.last_notice_sent_at is not None
            and (snap.now - snap.last_notice_sent_at)
            >= timedelta(hours=cfg.mandate_pre_debit_notice_hours)
        )
        if not notice_ok:
            blocked_actions.add(ActionType.RETRY)
            trace.append(GateTrace("G10_MANDATE_NOTICE", False,
                                   f"{cfg.mandate_pre_debit_notice_hours}h pre-debit notice not satisfied"))
        else:
            ok("G10_MANDATE_NOTICE")
    else:
        ok("G10_MANDATE_NOTICE", "not a mandate")

    # -- G11 AFA LIMIT -----------------------------------------------------
    # Above the AFA threshold a mandate debit needs fresh authentication. We
    # cannot silently pull the money; we can only ask.
    if snap.is_mandate:
        limit = cfg.afa_limit_for(snap.kind)
        if snap.amount_remaining > limit and not snap.afa_valid:
            blocked_actions.add(ActionType.RETRY)
            trace.append(GateTrace("G11_AFA", False,
                                   f"amount {snap.amount_remaining} > AFA limit {limit}, no valid AFA"))
        else:
            ok("G11_AFA", f"limit={limit}")
    else:
        ok("G11_AFA", "not a mandate")

    # -- G12 QUIET HOURS ---------------------------------------------------
    if in_quiet_hours(snap.now, cfg.quiet_start, cfg.quiet_end):
        blocked_actions.update(CONTACT_ACTIONS)
        until = next_contact_window(snap.now, cfg.quiet_start, cfg.quiet_end)
        if wait_until is None or until > wait_until:
            wait_until, wait_reason = until, StopReason.QUIET_HOURS
        trace.append(GateTrace("G12_QUIET_HOURS", False,
                               f"{snap.now.hour}:00 is inside quiet hours"))
    else:
        ok("G12_QUIET_HOURS")

    # -- G13 CONTACT CAP ---------------------------------------------------
    # A customer's patience is a budget, and it is shared across every case we
    # have with them.
    if snap.contacts_last_7d >= cfg.max_contacts_7d:
        blocked_actions.update(CONTACT_ACTIONS)
        trace.append(GateTrace("G13_CONTACT_CAP", False,
                               f"{snap.contacts_last_7d} contacts in 7d >= {cfg.max_contacts_7d}"))
    else:
        ok("G13_CONTACT_CAP", f"contacts_7d={snap.contacts_last_7d}")

    # -- G14 DUPLICATE PENDING ---------------------------------------------
    # Two webhooks for the same failure must not become two retries.
    if snap.pending_action_types:
        blocked_actions.update(snap.pending_action_types)
        trace.append(GateTrace("G14_DUPLICATE", False,
                               f"pending: {[a.value for a in snap.pending_action_types]}"))
    else:
        ok("G14_DUPLICATE")

    return GateResult(
        blocked=False,
        blocked_actions=frozenset(blocked_actions),
        wait_until=wait_until,
        wait_reason=wait_reason,
        trace=tuple(trace),
    )
