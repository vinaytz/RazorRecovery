"""
The experiment harness. Four arms, same cases, same seed, one code path.

Every arm goes through the real `run_gates` and the real `allocate`. Only the
policy function differs. That symmetry is the whole point: if the arms ran
different code, the comparison would prove nothing.

Time is virtual and advances in hourly ticks, so a 7-day recovery window takes
about a second.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from app.domain import policies
from app.domain.allocator import allocate
from app.domain.engine import decide as engine_decide
from app.domain.models import (
    CONTACT_ACTIONS,
    ActionType,
    Arm,
    CaseSnapshot,
    Config,
    Decision,
)
from app.services.bandit import Posterior
from sim.world import T0, Obligation, Truth, World

# Modelled difference, stated openly: the fixed-schedule baseline fires on
# schedule without re-reading payment state first. That is the common real-world
# pattern and it is where its false-chase rate comes from. Flip this to True and
# the false-chase gap closes -- our gate and uplift advantages remain.
BASELINE_RECHECKS_BEFORE_SEND = False


@dataclass
class CaseState:
    case_id: str
    ob: Obligation
    arm: Arm
    rung: int = 0
    attempts: int = 0
    status: str = "OPEN"          # OPEN|RECOVERED|SELF_RECOVERED|WRITTEN_OFF
    wake_at: datetime = T0
    last_action_at: datetime | None = None
    last_notice_sent_at: datetime | None = None
    promised_until: datetime | None = None
    settled: bool = False
    recovered_at: datetime | None = None
    contacts_sent: int = 0
    pending: Decision | None = None
    stopped: bool = False
    left_alone_counted: bool = False
    reversed_later: bool = False
    false_chases: int = 0
    actions_taken: int = 0


@dataclass
class ArmResult:
    arm: str
    cases: int = 0
    at_risk: int = 0
    gross_recovered: int = 0
    net_recovered: int = 0
    self_recovered: int = 0
    caused_recovered: int = 0
    contacts_sent: int = 0
    actions_taken: int = 0
    written_off_count: int = 0
    written_off_value: int = 0
    false_chases: int = 0
    # `double_charges` used to live here. It was removed in item 3d: no code path
    # ever incremented it, because nothing issues a server-initiated debit, so it
    # could only ever report 0 -- and a safety counter stuck at 0 reads as a
    # prevented risk. `app/metrics.py` now states the metric is inapplicable and
    # says why. Do not re-add it as a counter unless something can actually
    # increment it; `tests/test_dead_metrics.py` will fail if you do.
    left_alone_count: int = 0
    left_alone_value: int = 0
    by_segment: dict = field(default_factory=dict)

    @property
    def recovery_rate(self) -> float:
        return self.gross_recovered / self.at_risk if self.at_risk else 0.0

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "by_segment"}
        d["recovery_rate"] = round(self.recovery_rate, 4)
        return d


def snapshot_of(st: CaseState, w: World, cfg: Config, now: datetime,
                contacts_7d: int) -> CaseSnapshot:
    """Observable facts only. Nothing from Truth may ever appear here."""
    ob, c = st.ob, w.customer_of(st.ob)
    dt_end = w.in_downtime(ob.method, now)
    return CaseSnapshot(
        case_id=st.case_id, obligation_id=ob.id, customer_id=ob.customer_id,
        merchant_id="merchant_1", arm=st.arm,
        amount_due=ob.amount, amount_settled=ob.amount if st.settled else 0,
        kind=ob.kind, currency="INR",
        failure_class=ob.failure_class, method=ob.method, is_mandate=ob.is_mandate,
        now=now, opened_at=ob.opened_at, attempts=st.attempts, rung=st.rung,
        last_action_at=st.last_action_at, promised_until=st.promised_until,
        customer_tenure_days=c.tenure_days, customer_past_failures=c.past_failures,
        customer_past_recoveries=c.past_recoveries, contacts_last_7d=contacts_7d,
        opted_out=c.opted_out, risk_blocked=ob.risk_blocked,
        last_notice_sent_at=st.last_notice_sent_at, afa_valid=True,
        obligation_settled=st.settled,
        method_in_downtime=dt_end is not None, downtime_ends_at=dt_end,
        pending_action_types=(st.pending.action,) if st.pending else (),
    )


def oracle_decide_factory(w: World, cfg: Config):
    """Perfect play. Reads Truth. Upper bound only, never a peer competitor."""
    def _decide(snap: CaseSnapshot, cfg_: Config, posterior=None, rng=None) -> Decision:
        from app.domain.gates import run_gates
        gates = run_gates(snap, cfg_)
        if gates.blocked:
            return Decision(snap.case_id, gates.terminal_action or ActionType.NONE, None,
                            gates.stop_reason, gates.trace, (), snap, snap.now, cfg_.version)
        t = w.truth[snap.obligation_id]
        best, best_ev = None, 0.0
        for action, p in t.p_act.items():
            if action in gates.blocked_actions:
                continue
            ev = (p - t.p_none) * snap.amount_remaining - cfg_.cost_of(action) - cfg_.friction_of(action)
            if ev > best_ev:
                best, best_ev = action, ev
        if best is None:
            return Decision(snap.case_id, ActionType.WAIT, snap.now + timedelta(hours=6),
                            None, gates.trace, (), snap, snap.now, cfg_.version)
        return Decision(snap.case_id, best, snap.now, None, gates.trace, (), snap,
                        snap.now, cfg_.version, notes="oracle")
    return _decide


POLICIES = {
    Arm.CONTROL: policies.control_decide,
    Arm.BASELINE: policies.baseline_decide,
    Arm.ENGINE: engine_decide,
}

# Each arm needs its own stream so one arm's draws cannot shift another's, but the
# offset MUST be stable across processes. `hash(str)` is salted per interpreter
# (PYTHONHASHSEED), so using it here silently reseeded every arm on every run and
# `--seed` controlled nothing. Explicit integers, deterministic forever.
ARM_SEED_OFFSET = {
    Arm.CONTROL: 0,
    Arm.BASELINE: 1,
    Arm.ENGINE: 2,
    Arm.ORACLE: 3,
}


def run_arm(w: World, arm: Arm, cfg: Config, posterior=None,
            seed: int = 42, recorder=None) -> tuple[ArmResult, list[int]]:
    """Run one arm against its OWN copy of the world.

    The deep copy is load-bearing, not defensive tidiness. `_execute` writes
    `tr.self_pay_at = None` on a sleeping dog -- the customer who would have paid
    on their own until we reminded them they wanted to cancel. That is a mutation
    of `World`, and the four arms run in sequence over one world object, so
    without this copy BASELINE's kills are still missing when ENGINE starts and
    both are missing when ORACLE starts. The arms stop being four independent
    draws on the same world and become a chain, with every arm inheriting the
    damage done by the ones before it.

    It was measured before it was fixed (item 3z): the contamination was worth
    Rs 23.6k of incremental and 1.9 points of ceiling share, and it flowed in the
    flattering direction, because ENGINE was scored on a world where BASELINE had
    already burned two of the self-payers ENGINE would otherwise have had to
    resist contacting.

    The copy lives HERE rather than in `run_once` so a caller cannot forget it.
    `tests/test_arm_independence.py` pins the property from the outside: an arm
    run alone must produce byte-identical results to the same arm run fourth.
    """
    w = copy.deepcopy(w)
    rng = np.random.default_rng(seed * 10 + ARM_SEED_OFFSET[arm])
    policy = oracle_decide_factory(w, cfg) if arm == Arm.ORACLE else POLICIES[arm]

    states = {ob.id: CaseState(f"{arm.value}_{ob.id}", ob, arm, wake_at=ob.opened_at)
              for ob in w.obligations}
    contacts: dict[str, list[datetime]] = {}
    res = ArmResult(arm=arm.value, cases=len(states),
                    at_risk=sum(ob.amount for ob in w.obligations))

    horizon = T0 + timedelta(hours=cfg.window_hours + 48)
    t = T0
    while t <= horizon:
        # 1. execute anything due. ORDER MATTERS: actions fire before we book the
        #    self-payment, because in the real world the webhook has not landed
        #    yet. Whether an arm discovers the payment first is exactly the
        #    difference between a state re-check and a fixed schedule.
        for st in states.values():
            if st.pending is None or st.status != "OPEN":
                continue
            d = st.pending
            if d.execute_at and d.execute_at > t:
                continue
            _execute(st, d, w, cfg, rng, res, posterior, t, contacts)
            st.pending = None

        # 2. money that arrives on its own, with no help from us
        for st in states.values():
            if st.status != "OPEN":
                continue
            tr = w.truth[st.ob.id]
            if tr.self_pay_at and tr.self_pay_at <= t:
                st.status, st.settled, st.recovered_at = "SELF_RECOVERED", True, t
                st.reversed_later = tr.reversed_later

        # 3. decide for every case that is awake and idle
        due = [st for st in states.values()
               if st.status == "OPEN" and st.pending is None and not st.stopped
               and st.ob.opened_at <= t and st.wake_at <= t]

        if due:
            proposals = []
            for st in due[: max(cfg.batch_size, len(due))]:
                snap = snapshot_of(st, w, cfg, t, _contacts_7d(contacts, st.ob.customer_id, t))
                proposals.append((st, policy(snap, cfg, posterior, rng)))

            allocated = allocate([d for _, d in proposals], cfg)
            by_id = {d.case_id: d for d in allocated}

            for st, orig in proposals:
                d = by_id.get(orig.case_id, orig)
                if recorder is not None:
                    recorder(st, d)
                if d.action in (ActionType.NONE, ActionType.WRITE_OFF):
                    # We stop acting. The obligation stays open: they may still
                    # pay on their own, and that money is NOT ours to claim.
                    st.stopped = True
                    st.wake_at = horizon + timedelta(hours=1)
                    if (d.stop_reason and d.stop_reason.value == "EV_NEGATIVE"
                            and not st.left_alone_counted):
                        st.left_alone_counted = True
                        res.left_alone_count += 1
                        res.left_alone_value += st.ob.amount
                elif d.action == ActionType.WAIT:
                    st.wake_at = d.execute_at or (t + timedelta(hours=4))
                    if (d.stop_reason and d.stop_reason.value == "EV_NEGATIVE"
                            and not st.left_alone_counted):
                        st.left_alone_counted = True
                        res.left_alone_count += 1
                        res.left_alone_value += st.ob.amount
                else:
                    st.pending = d
                    st.wake_at = d.execute_at or t

        t += timedelta(hours=1)

    # 4. window closes
    for st in states.values():
        if st.status == "OPEN":
            _close(st, res, "WRITTEN_OFF", w)
        if st.status in ("RECOVERED", "SELF_RECOVERED"):
            res.gross_recovered += st.ob.amount
            if not st.reversed_later:
                res.net_recovered += st.ob.amount
            if st.status == "SELF_RECOVERED":
                res.self_recovered += st.ob.amount
            else:
                res.caused_recovered += st.ob.amount
        seg = f"{st.ob.failure_class.value}"
        s = res.by_segment.setdefault(seg, {"at_risk": 0, "recovered": 0, "n": 0})
        s["at_risk"] += st.ob.amount
        s["n"] += 1
        if st.status in ("RECOVERED", "SELF_RECOVERED"):
            s["recovered"] += st.ob.amount

    # 5. the counterfactual arm feeds the bandit's do-nothing estimate
    if arm == Arm.CONTROL and posterior is not None:
        for st in states.values():
            snap = snapshot_of(st, w, cfg, T0 + timedelta(hours=1), 0)
            posterior.update(snap.segment, ActionType.NONE, st.status == "SELF_RECOVERED")

    if recorder is not None:
        recorder.finish(states) if hasattr(recorder, "finish") else None

    per_case = [st.ob.amount if states[ob.id].status in ("RECOVERED", "SELF_RECOVERED") else 0
                for ob in w.obligations for st in [states[ob.id]]]
    return res, per_case


def _contacts_7d(contacts: dict, cust: str, now: datetime) -> int:
    xs = contacts.get(cust, [])
    return sum(1 for x in xs if now - x <= timedelta(days=7))


def _close(st: CaseState, res: ArmResult, status: str, w: World) -> None:
    if st.status == "OPEN":
        st.status = status
        if status == "WRITTEN_OFF":
            res.written_off_count += 1
            res.written_off_value += st.ob.amount


def _execute(st: CaseState, d: Decision, w: World, cfg: Config, rng,
             res: ArmResult, posterior, t: datetime, contacts: dict) -> None:
    tr: Truth = w.truth[st.ob.id]
    is_contact = d.action in CONTACT_ACTIONS

    # The re-check. You can abort a retry; you cannot unsend an SMS.
    rechecks = st.arm != Arm.BASELINE or BASELINE_RECHECKS_BEFORE_SEND
    already = tr.self_pay_at is not None and tr.self_pay_at <= t
    if already:
        if rechecks:
            st.status, st.settled, st.recovered_at = "SELF_RECOVERED", True, t
            st.reversed_later = tr.reversed_later
            return                                   # aborted. nothing sent.
        if is_contact:
            res.false_chases += 1                    # asked a paying customer to pay
            st.false_chases += 1
            contacts.setdefault(st.ob.customer_id, []).append(t)
            res.contacts_sent += 1
        st.status, st.settled, st.recovered_at = "SELF_RECOVERED", True, t
        st.reversed_later = tr.reversed_later
        return

    res.actions_taken += 1
    st.actions_taken += 1
    st.last_action_at = t
    if d.action == ActionType.RETRY:
        st.attempts += 1
    if is_contact:
        st.contacts_sent += 1
        res.contacts_sent += 1
        contacts.setdefault(st.ob.customer_id, []).append(t)
        st.last_notice_sent_at = t

    inc = max(0.0, tr.p_act.get(d.action, tr.p_none) - tr.p_none)
    caused = bool(rng.random() < inc)

    if caused:
        st.status, st.settled, st.recovered_at = "RECOVERED", True, t
        st.reversed_later = tr.reversed_later
    else:
        # sleeping dogs: the nudge reminds them they wanted to cancel.
        # This MUTATES the world. Safe only because `run_arm` deep-copies it per
        # arm -- see the docstring there. Do not remove that copy.
        u = tr.uplift(d.action)
        if u < 0 and rng.random() < abs(u) and tr.self_pay_at and tr.self_pay_at > t:
            tr.self_pay_at = None

    # What the engine can actually observe: the obligation settled soon after we
    # acted. It cannot tell whether it caused that. This attribution noise is
    # exactly why scoring on uplift rather than raw success matters.
    observed = caused or (tr.self_pay_at is not None and tr.self_pay_at <= t + timedelta(hours=24))
    if posterior is not None and st.arm == Arm.ENGINE:
        snap = snapshot_of(st, w, cfg, t, 0)
        posterior.update(snap.segment, d.action, observed)

    st.rung = max(st.rung, _rung_of(d.action))
    st.wake_at = t + timedelta(hours=2)


def _rung_of(action: ActionType) -> int:
    from app.domain.models import RUNG_ORDER
    try:
        return RUNG_ORDER.index(action)
    except ValueError:
        return 0
