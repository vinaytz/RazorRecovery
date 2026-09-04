"""
Expected-value scoring, on UPLIFT rather than raw success probability.

The distinction is the whole product:

    p_act  = P(they pay | we take this action)
    p_none = P(they pay | we do nothing)
    uplift = p_act - p_none          <- may be negative. that is the point.

Scoring on p_act rewards the engine for chasing customers who were going to pay
anyway. Scoring on uplift does not. It also makes "sleeping dogs" -- customers a
reminder pushes toward cancelling -- fall out naturally as negative EV, so the
engine learns to leave them alone instead of being told to.
"""
from __future__ import annotations

from app.domain.models import (
    ActionType,
    CaseSnapshot,
    Config,
    ScoredAction,
)


def friction_multiplier(snap: CaseSnapshot) -> float:
    """Annoyance compounds. The 4th message this week costs far more than the 1st."""
    return 1.0 + 0.6 * snap.contacts_last_7d


def score(snap: CaseSnapshot, action: ActionType, posterior, cfg: Config, rng) -> ScoredAction:
    if action == ActionType.WAIT:
        return ScoredAction(ActionType.WAIT, 0.0, 0.0, 0.0, 0.0, 0, 0.0)

    p_act = posterior.sample(snap.segment, action, rng)
    p_none = posterior.sample(snap.segment, ActionType.NONE, rng)
    uplift = p_act - p_none

    cost = cfg.cost_of(action)
    friction = cfg.friction_of(action) * friction_multiplier(snap)
    ev = uplift * snap.amount_remaining - cost - friction

    return ScoredAction(
        action=action, ev=ev, p_act=p_act, p_none=p_none,
        uplift=uplift, cost=cost, friction=friction,
    )


def score_all(snap: CaseSnapshot, actions, posterior, cfg: Config, rng) -> list[ScoredAction]:
    scored = [score(snap, a, posterior, cfg, rng) for a in actions]
    scored.sort(key=lambda s: s.ev, reverse=True)
    return scored
