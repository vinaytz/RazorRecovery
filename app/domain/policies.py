"""
The opponents. Same signature as engine.decide(), so the harness treats all four
arms identically -- that symmetry is what makes the comparison fair.

  CONTROL  never acts. the holdout. measures how much money arrives on its own.
  BASELINE fixed retry schedule then one reminder. context-blind, like most
           recovery tooling. this is the honest opponent, not a strawman:
           it still respects the hard gates, so it never chases a settled
           obligation and never breaks a compliance rule.
  ENGINE   app/domain/engine.decide
  ORACLE   lives in sim/, because it needs ground truth. upper bound only.
"""
from __future__ import annotations

from datetime import timedelta

from app.domain.gates import run_gates
from app.domain.models import (
    ActionType,
    CaseSnapshot,
    Config,
    Decision,
    StopReason,
)


def _decision(snap, action, cfg, gates, execute_at=None, reason=None, notes="") -> Decision:
    return Decision(
        case_id=snap.case_id, action=action, execute_at=execute_at, stop_reason=reason,
        gate_trace=gates.trace, candidates=(), snapshot=snap, decided_at=snap.now,
        config_version=cfg.version, notes=notes,
    )


def control_decide(snap: CaseSnapshot, cfg: Config, posterior=None, rng=None) -> Decision:
    gates = run_gates(snap, cfg)
    return _decision(snap, ActionType.NONE, cfg, gates,
                     reason=StopReason.CONTROL_ARM, notes="holdout: never act")


def baseline_decide(snap: CaseSnapshot, cfg: Config, posterior=None, rng=None) -> Decision:
    """Retry at T+0h, T+1h, T+24h, then one reminder, then stop."""
    gates = run_gates(snap, cfg)
    if gates.blocked:
        return _decision(snap, gates.terminal_action or ActionType.NONE, cfg, gates,
                         reason=gates.stop_reason, notes="stopped by gate")
    if gates.wait_until and gates.wait_until > snap.now:
        return _decision(snap, ActionType.WAIT, cfg, gates,
                         execute_at=gates.wait_until, reason=gates.wait_reason)

    schedule = cfg.baseline_retry_schedule_hours

    if snap.attempts < len(schedule) and ActionType.RETRY not in gates.blocked_actions:
        due = snap.opened_at + timedelta(hours=schedule[snap.attempts])
        at = max(due, snap.now)
        return _decision(snap, ActionType.RETRY, cfg, gates, execute_at=at,
                         notes=f"fixed schedule step {snap.attempts + 1}/{len(schedule)}")

    if snap.rung < 2 and ActionType.REMIND not in gates.blocked_actions:
        return _decision(snap, ActionType.REMIND, cfg, gates,
                         execute_at=snap.now + timedelta(hours=2),
                         notes="schedule exhausted, one reminder")

    return _decision(snap, ActionType.NONE, cfg, gates,
                     reason=StopReason.BUDGET_EXHAUSTED, notes="baseline exhausted")
