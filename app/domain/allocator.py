"""
Batch allocation.

Recovery is not N independent decisions. A customer's attention is a shared,
finite budget: spend it on 400 small cases and there is nothing left for the
large ones. So proposals compete, ranked by EV per unit of contact, and
everything below the line becomes WAIT.

Pure. Operates on a whole batch at once.
"""
from __future__ import annotations

from app.domain.models import (
    ActionType,
    Config,
    Decision,
    StopReason,
)


def allocate(decisions: list[Decision], cfg: Config) -> list[Decision]:
    budget = cfg.global_contact_budget_per_tick

    contenders: list[tuple[float, int, Decision]] = []
    passthrough: list[Decision] = []

    for i, d in enumerate(decisions):
        used = cfg.contacts_for(d.action)
        if used <= 0:
            passthrough.append(d)          # silent actions never compete
            continue
        ev = d.candidates[0].ev if d.candidates else 0.0
        contenders.append((ev / used, i, d))

    contenders.sort(key=lambda x: x[0], reverse=True)

    out: list[Decision] = list(passthrough)
    spent = 0
    for _, _, d in contenders:
        used = cfg.contacts_for(d.action)
        if spent + used <= budget:
            out.append(d)
            spent += used
        else:
            out.append(d.downgraded_to_wait(StopReason.CONTACT_BUDGET, d.execute_at))

    order = {d.case_id: i for i, d in enumerate(decisions)}
    out.sort(key=lambda d: order.get(d.case_id, 0))
    return out
