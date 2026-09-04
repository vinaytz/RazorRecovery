"""
The escalation ladder.

Razorpay's Track 03 bar asks for "compliant escalation". An escalation is not a
bag of actions you pick from; it is an ordered climb with permission required at
each rung and a real top.

Rules:
  - climb one rung at a time
  - never descend
  - never jump past a rung that is merely unattractive
  - DO skip a rung the gates have permanently blocked (an expired card can never
    be fixed by RETRY, so the case would otherwise be trapped at rung 1 forever)
  - the ladder has a top, and the top is a recorded WRITE_OFF
"""
from __future__ import annotations

from app.domain.models import (
    RUNG_ORDER,
    ActionType,
    CaseSnapshot,
    Config,
    GateResult,
)


def legal_next_rungs(snap: CaseSnapshot, gates: GateResult, cfg: Config) -> list[ActionType]:
    """Return the candidate actions for this case, cheapest first.

    Always includes WAIT: doing nothing right now is a legal, scored option.
    """
    candidates: list[ActionType] = [ActionType.WAIT]

    top = min(cfg.max_rung, len(RUNG_ORDER) - 1)
    for rung in range(snap.rung + 1, top + 1):
        action = RUNG_ORDER[rung]
        if action in gates.blocked_actions:
            # DECISION: skip over a blocked rung rather than trapping the case.
            # This is not "jumping the ladder" -- the rung was made illegal by a
            # hard gate, not skipped for convenience.
            continue
        if action == ActionType.WRITE_OFF:
            continue  # write-off is a terminal state, never a scored candidate
        candidates.append(action)
        break

    return candidates


def rung_of(action: ActionType) -> int:
    try:
        return RUNG_ORDER.index(action)
    except ValueError:
        return 0
