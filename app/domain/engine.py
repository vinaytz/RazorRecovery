"""
decide() -- the pure core.

    decide(snapshot, config, posterior, rng) -> Decision

No database. No HTTP. No clock. No LLM. Everything arrives as an argument.

That constraint is not fussiness. It is what buys:
  - replay: store the snapshot, re-run this, get a bit-identical answer
  - the audit trail: the inputs and the trace ARE the record, not a log line
  - speed: thousands of cases per second, so the benchmark is cheap
  - four arms: control / baseline / engine / oracle share one signature

If you ever need a fact in here, put it on CaseSnapshot. Do not fetch it.
"""
from __future__ import annotations

from app.domain import ladder, scoring, timing
from app.domain.gates import run_gates
from app.domain.models import (
    ActionType,
    CaseSnapshot,
    Config,
    Decision,
    ScoredAction,
    StopReason,
)


def decide(snap: CaseSnapshot, cfg: Config, posterior, rng) -> Decision:
    gates = run_gates(snap, cfg)

    def build(action: ActionType, *, execute_at=None, reason=None,
              candidates: tuple[ScoredAction, ...] = (), notes: str = "") -> Decision:
        return Decision(
            case_id=snap.case_id, action=action, execute_at=execute_at,
            stop_reason=reason, gate_trace=gates.trace, candidates=candidates,
            snapshot=snap, decided_at=snap.now, config_version=cfg.version, notes=notes,
        )

    # 1. A hard gate ends the case. Nothing downstream may override this.
    if gates.blocked:
        terminal = gates.terminal_action or ActionType.NONE
        return build(terminal, reason=gates.stop_reason,
                     notes=f"stopped by gate: {gates.stop_reason.value if gates.stop_reason else ''}")

    # 2. Legal but not yet -- a downtime, quiet hours, or a promise to pay.
    if gates.wait_until is not None and gates.wait_until > snap.now:
        return build(ActionType.WAIT, execute_at=gates.wait_until,
                     reason=gates.wait_reason, notes="deferred by gate")

    # 3. What may we do next on the ladder?
    candidates = ladder.legal_next_rungs(snap, gates, cfg)
    real_actions = [a for a in candidates if a != ActionType.WAIT]
    if not real_actions:
        return build(ActionType.WAIT, execute_at=timing.when(snap, ActionType.WAIT, gates, cfg),
                     reason=StopReason.NO_LEGAL_ACTION, notes="no legal rung available")

    # 4. Score on uplift.
    scored = tuple(scoring.score_all(snap, real_actions, posterior, cfg, rng))
    best = scored[0]

    # 5. Doing nothing is a valid, scored outcome -- not a failure to decide.
    if best.ev <= 0:
        return build(ActionType.WAIT,
                     execute_at=timing.when(snap, ActionType.WAIT, gates, cfg),
                     reason=StopReason.EV_NEGATIVE, candidates=scored,
                     notes=f"best EV {best.ev:.0f} <= 0; leaving this customer alone")

    return build(best.action, execute_at=timing.when(snap, best.action, gates, cfg),
                 candidates=scored, notes=f"uplift {best.uplift:+.3f}, EV {best.ev:.0f}")
