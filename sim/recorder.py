"""
Captures decisions during a benchmark so the dashboard has something to show.

We do not store all of them -- 2,000 cases x 4 arms x 168 ticks is a lot of rows
for no benefit. We store:
  - every decision for a sample of obligations, so a case history is complete
  - "highlights": the decisions that make the demo, found automatically
      sleeping_dog  large amount, EV negative, deliberately left alone
      gate_stop     blocked before any scoring happened
      write_off     the ladder ran out, recorded honestly
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from app.domain.models import ActionType, Arm, StopReason


class Recorder:
    def __init__(self, run_id: str, sample_obligation_ids: set[str],
                 highlight_cap: int = 40):
        self.run_id = run_id
        self.sample = sample_obligation_ids
        self.cap = highlight_cap
        self.decisions: list[tuple] = []
        self.cases: list[tuple] = []
        self._hl: dict[str, int] = {}

    def _highlight(self, st, d) -> str | None:
        reason = d.stop_reason.value if d.stop_reason else None
        amount = st.ob.amount

        if reason == StopReason.EV_NEGATIVE.value and amount >= 300_000:
            kind = "sleeping_dog"
        elif d.action == ActionType.WRITE_OFF:
            kind = "write_off"
        elif reason in (StopReason.ALREADY_SETTLED.value,
                        StopReason.MANDATE_NOTICE_REQUIRED.value,
                        StopReason.AFA_REQUIRED.value,
                        StopReason.OPTED_OUT.value,
                        StopReason.WINDOW_EXPIRED.value):
            kind = "gate_stop"
        elif d.action in (ActionType.METHOD_CHANGE, ActionType.PAY_LINK) and amount >= 500_000:
            kind = "big_save"
        else:
            return None

        if self._hl.get(kind, 0) >= self.cap:
            return None
        self._hl[kind] = self._hl.get(kind, 0) + 1
        return kind

    def __call__(self, st, d) -> None:
        hl = self._highlight(st, d)
        if st.ob.id not in self.sample and hl is None:
            return
        self.decisions.append((
            f"dec_{uuid.uuid4().hex[:16]}", self.run_id, st.case_id,
            d.decided_at.isoformat(), d.action.value,
            d.stop_reason.value if d.stop_reason else None,
            json.dumps(d.snapshot.to_json()),
            json.dumps([t.as_dict() for t in d.gate_trace]),
            json.dumps([c.as_dict() for c in d.candidates]),
            d.config_version, d.notes, hl,
        ))

    def finish(self, states: dict) -> None:
        for st in states.values():
            if st.ob.id not in self.sample and not any(
                    r[2] == st.case_id for r in self.decisions):
                continue
            self.cases.append((
                st.case_id, self.run_id, st.ob.id, st.ob.customer_id, st.arm.value,
                st.ob.amount, st.ob.failure_class.value, st.ob.method, st.ob.kind.value,
                st.rung, st.attempts, st.status, st.contacts_sent, st.actions_taken,
                st.ob.opened_at.isoformat(),
                st.recovered_at.isoformat() if st.recovered_at else None,
            ))
