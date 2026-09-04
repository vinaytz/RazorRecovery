"""
The live worker. What the benchmark's tick loop is, for real cases.

`sim/runner.py` walks 2000 cases through virtual time in a couple of seconds. This
walks the cases in the database through real time, one second at a time, and it is
deliberately the same three steps in the same order:

    1. DECIDE   build a snapshot from the db, call the same `decide()`
    2. SCHEDULE write the chosen action with an idempotency key
    3. EXECUTE  run whatever is due, through the same executor

Nothing here is a second implementation of the engine. The snapshot is assembled
from stored rows and handed to the identical pure function the benchmark scores,
which is why a live decision can be replayed on the dashboard next to a simulated
one and the gate trace reads the same.

TIME_SCALE, and the one thing it is not allowed to touch.

A recovery ladder is measured in hours -- wait 6 hours, then remind, then a link
tomorrow. A demo is measured in seconds. `TIME_SCALE` divides scheduled DELAYS on
the live path so a 6-hour wait becomes 6 seconds at TIME_SCALE=3600, and the whole
ladder is watchable while somebody films it.

It divides the delay, not the clock. `now` stays real: timestamps in the database
remain honest, `contacts_last_7d` still means seven real days, and a case's
`opened_at` is when it actually opened. Only the gap between deciding and acting
compresses. That is the difference between speeding up a demo and lying in one.

It touches nothing the benchmark reads. `sim/runner.py` has its own VirtualClock
and never imports this module; TIME_SCALE is read here and nowhere else, so no
setting of it can move the benchmark md5. `tests/test_live_worker.py` pins that.

Why a worker at all, when the ingest path could decide inline: because most of
what a recovery engine does is WAIT. The decision "remind them in six hours" is
worthless unless something comes back six hours later, and nothing in a webhook
handler ever comes back.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from app.config_loader import load_config
from app.controllers import execute as ex
from app.domain.engine import decide
from app.domain.models import (Arm, CaseSnapshot, Decision, FailureClass,
                               ObligationKind, ActionType)
from app.repos import store
from app.services.bandit import Posterior

log = logging.getLogger("razorrecovery.live")

# Actions that finish a case rather than doing something to a customer.
TERMINAL = {ActionType.WRITE_OFF, ActionType.HUMAN}


def time_scale() -> float:
    """`TIME_SCALE`. 1 is real time. 3600 makes one simulated hour take a second.

    Read per call so the demo can change pace without a restart. Anything
    unparseable, zero, or negative falls back to 1 -- a bad value must slow the
    system to real time, never turn every scheduled delay into "now".
    """
    try:
        v = float(os.environ.get("TIME_SCALE", "1"))
    except (TypeError, ValueError):
        return 1.0
    return v if v > 0 else 1.0


def scaled(execute_at: datetime, now: datetime, scale: float | None = None) -> datetime:
    """Bring a scheduled time forward by TIME_SCALE. Never pushes it later.

    `max(0, ...)` matters: an action already due stays due. Dividing a negative
    delay would send an overdue action into the future, which is how a demo
    silently stops doing anything.
    """
    s = time_scale() if scale is None else scale
    delay = max(0.0, (execute_at - now).total_seconds())
    return now + timedelta(seconds=delay / s)


class LiveWorker:
    """One decide-and-execute pass over the live cases, callable on a timer.

    Holds the posterior in memory. A restart loses what the live path has learned,
    which is honest about what this is: the bandit's real training signal is the
    benchmark, and live volume in a hackathon demo is a handful of cases. Item 3c
    revisits the learning; persisting a posterior fitted on nine cases would look
    like learning while being noise.
    """

    def __init__(self, con, executor, cfg=None, posterior=None, rng=None):
        import numpy as np                              # noqa: PLC0415

        self.con = con
        self.executor = executor
        self.cfg = cfg or load_config()
        self.posterior = posterior if posterior is not None else Posterior(self.cfg)
        # Seeded. Thompson sampling draws from this, and an unseeded live worker
        # would make the same case decide differently on a re-run for no reason a
        # judge could see. Invariant 7 applies here too.
        self.rng = rng if rng is not None else np.random.default_rng(1729)

    # -- the pass ---------------------------------------------------------

    def tick(self, now: datetime | None = None, limit: int = 50) -> dict:
        now = now or datetime.now()
        scale = time_scale()
        decided = [self.decide_case(row, now, scale) for row in self.open_cases(limit)]
        executed = self.execute_due(now)
        return {
            "at": now.isoformat(), "time_scale": scale,
            "decided": [d for d in decided if d], "executed": executed,
            "verdict": (f"{len([d for d in decided if d])} decision(s), "
                        f"{len(executed)} action(s) executed"),
        }

    def open_cases(self, limit: int = 50) -> list:
        """Live cases with no action already in flight.

        `pending_action_types` on the snapshot is how the engine learns it has
        already acted; excluding those cases here as well means a slow tick cannot
        stack a second decision on top of an unexecuted first one.
        """
        return list(self.con.execute(
            "SELECT * FROM cases WHERE run_id = 'live' AND status = 'OPEN'"
            " AND case_id NOT IN (SELECT case_id FROM actions"
            "   WHERE status IN ('PENDING', 'IN_FLIGHT', 'UNKNOWN'))"
            " ORDER BY amount DESC LIMIT ?", (limit,)))

    def decide_case(self, case_row, now: datetime, scale: float) -> dict | None:
        snap = self.snapshot(case_row, now)
        if snap is None:
            return None

        d = decide(snap, self.cfg, self.posterior, self.rng)
        self.record(d, case_row, now)

        if d.action in (ActionType.NONE, ActionType.WAIT):
            # Nothing to schedule. WAIT is a real answer -- the next tick asks again,
            # and the gate that deferred it will say when it stops deferring.
            return {"case_id": snap.case_id, "action": d.action.value,
                    "stop_reason": d.stop_reason.value if d.stop_reason else None,
                    "scheduled_for": None,
                    "why": d.notes or "nothing to do on this case yet"}

        if d.action in TERMINAL:
            status = "WRITTEN_OFF" if d.action is ActionType.WRITE_OFF else "ESCALATED"
            self.con.execute("UPDATE cases SET status = ?, closed_at = ? WHERE case_id = ?",
                             (status, now.isoformat(), snap.case_id))
            self.con.commit()
            return {"case_id": snap.case_id, "action": d.action.value,
                    "stop_reason": d.stop_reason.value if d.stop_reason else None,
                    "scheduled_for": None,
                    "why": f"case closed {status} -- no customer contact"}

        at = scaled(d.execute_at or now, now, scale)
        aid = ex.schedule(self.con, snap.case_id, snap.obligation_id, d.action, at)
        self.con.execute("UPDATE cases SET actions_taken = actions_taken + 1,"
                         " rung = ? WHERE case_id = ?",
                         (self.rung_of(d.action, case_row["rung"]), snap.case_id))
        self.con.commit()
        return {"case_id": snap.case_id, "action": d.action.value, "action_id": aid,
                "scheduled_for": at.isoformat(),
                "real_delay_seconds": round((at - now).total_seconds(), 1),
                "why": d.notes or "chosen on expected uplift"}

    def execute_due(self, now: datetime) -> list[dict]:
        out = []
        for row in ex.due_actions(self.con, now):
            res = ex.run(self.con, row, self.executor)
            res["type"] = row["type"]
            if res.get("contact_sent"):
                self.con.execute(
                    "UPDATE cases SET contacts_sent = contacts_sent + 1 WHERE case_id = ?",
                    (row["case_id"],))
            if res.get("reason") == "ALREADY_SETTLED":
                # The re-check in `ex.run` caught a payment we had not matched yet.
                # The case is not ours to close from here -- the settlement path owns
                # that, with a match level. All we do is stop.
                log.info("action %s aborted: %s had already been paid",
                         row["action_id"], row["obligation_id"])
            self.con.commit()
            out.append(res)
        return out

    # -- the snapshot -----------------------------------------------------

    def snapshot(self, case_row, now: datetime) -> CaseSnapshot | None:
        """Assemble what the engine is allowed to know, from stored rows only.

        Every field is either on the case, on the obligation, or counted from the
        contacts ledger. Nothing is inferred and nothing is invented: a field we
        cannot source live gets its conservative value, not a plausible one. The
        two that matter are `method_in_downtime` and `promised_until`, both False /
        None today because nothing populates them yet -- items 3b and 3e. A comment
        is the honest placeholder; a guess would be a silent one.
        """
        ob = self.con.execute("SELECT * FROM obligations WHERE id = ?",
                              (case_row["obligation_id"],)).fetchone()
        if ob is None:
            log.warning("case %s has no obligation row -- skipping", case_row["case_id"])
            return None

        pending = tuple(
            ActionType(r["type"]) for r in self.con.execute(
                "SELECT DISTINCT type FROM actions WHERE case_id = ?"
                " AND status IN ('PENDING', 'IN_FLIGHT', 'UNKNOWN')",
                (case_row["case_id"],)))
        last = self.con.execute(
            "SELECT MAX(sent_at) AS t FROM contacts WHERE obligation_id = ? AND ok = 1",
            (case_row["obligation_id"],)).fetchone()
        last_contact = datetime.fromisoformat(last["t"]) if last and last["t"] else None

        return CaseSnapshot(
            case_id=case_row["case_id"], obligation_id=ob["id"],
            customer_id=ob["customer_id"] or case_row["customer_id"],
            merchant_id=os.getenv("MERCHANT_ID", "merchant_1"),
            arm=Arm.ENGINE,
            amount_due=int(ob["amount_due"] or 0),
            amount_settled=int(ob["amount_settled"] or 0),
            kind=ObligationKind(case_row["kind"]), currency="INR",
            failure_class=FailureClass(case_row["failure_class"]),
            method=case_row["method"] or "card",
            # Only a subscription holds a mandate we could charge again. An order
            # does not, and assuming otherwise is what makes RETRY look free.
            is_mandate=(case_row["kind"] == ObligationKind.SUBSCRIPTION.value),
            now=now, opened_at=datetime.fromisoformat(case_row["opened_at"]),
            attempts=int(case_row["attempts"] or 0), rung=int(case_row["rung"] or 0),
            last_action_at=last_contact,
            promised_until=None,          # item 3e sets this from a customer's reply
            customer_tenure_days=self.tenure_days(ob, now),
            customer_past_failures=self.past(ob["customer_id"], "OPEN"),
            customer_past_recoveries=self.past(ob["customer_id"], "RECOVERED"),
            contacts_last_7d=store.contacts_last_7d(self.con, ob["customer_id"], now),
            opted_out=False,              # item 3e sets this from an OPT_OUT reply
            risk_blocked=False,
            last_notice_sent_at=last_contact,
            afa_valid=True,
            obligation_settled=(ob["status"] == "SETTLED"),
            method_in_downtime=False,     # item 3b populates this from the downtime feed
            downtime_ends_at=None,
            pending_action_types=pending,
        )

    def tenure_days(self, ob, now: datetime) -> int:
        """How long we have known this customer, from their oldest obligation."""
        r = self.con.execute("SELECT MIN(opened_at) AS t FROM obligations WHERE customer_id = ?",
                             (ob["customer_id"],)).fetchone()
        if not r or not r["t"]:
            return 0
        return max(0, (now - datetime.fromisoformat(r["t"])).days)

    def past(self, customer_id: str, status: str) -> int:
        like = "RECOVERED" if status == "RECOVERED" else status
        return int(self.con.execute(
            "SELECT COUNT(*) FROM cases WHERE customer_id = ? AND status LIKE ?"
            " AND run_id = 'live'", (customer_id, f"%{like}%")).fetchone()[0])

    def rung_of(self, action: ActionType, current: int) -> int:
        """The ladder only ever climbs. `max` is the guard, not a convention."""
        from app.domain.models import RUNG_ORDER            # noqa: PLC0415
        try:
            return max(int(current or 0), RUNG_ORDER.index(action))
        except ValueError:
            return int(current or 0)

    # -- the audit trail --------------------------------------------------

    def record(self, d: Decision, case_row, now: datetime) -> None:
        """Store the decision so the dashboard can replay it.

        Same table, same shape as a benchmark decision. That is what lets the
        Decision tab show a live case and a simulated one side by side and prove
        the engine did not behave differently because it was being watched.
        """
        import json
        import uuid

        store.save_decisions(self.con, [(
            f"dec_{uuid.uuid4().hex[:12]}", "live", d.case_id, now.isoformat(),
            d.action.value, d.stop_reason.value if d.stop_reason else None,
            json.dumps(d.snapshot.to_json()),
            json.dumps([g.as_dict() for g in d.gate_trace]),
            json.dumps([c.as_dict() for c in d.candidates]),
            d.config_version, d.notes, None,
        )])
