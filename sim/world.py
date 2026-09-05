"""
The synthetic world, and its hidden truth.

Read this before trusting any number the benchmark prints.

The engine never sees anything in this file. It sees CaseSnapshot, which carries
only observable facts: failure class, amount, attempt count, method, contact
history. The latent traits below -- p_self, responsiveness -- are the ground
truth the engine has to *learn* from outcomes, exactly as it would in production.

The honesty problem: we wrote this world, so of course our engine does well in
it. Two defences, both required.
  1. Presets. At least one world must be built so the dumb baseline nearly ties
     us. Report it. Do not tune it away. `remind_friendly` is that world today;
     `retry_friendly` was, until item 3a blocked retries on non-mandate cases and
     shrank its reach to the ~25% of cases holding a mandate. Both are kept, and
     which is doing the work is stated rather than left to be assumed.
     `link_friendly` is NOT one of these -- it doubles our own best levers and
     flatters us. It is labelled as such where it is defined.
  2. The engine may only learn through outcomes. If any code path lets it read
     `Truth`, the benchmark is worthless.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from app.domain.models import ActionType, FailureClass, ObligationKind

T0 = datetime(2026, 3, 2, 0, 0, 0)


# --------------------------------------------------------------------------
# Archetypes -- the four uplift quadrants, made concrete
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Archetype:
    name: str
    share: float
    p_self: float          # pays on their own inside the window
    responsiveness: float  # multiplier on an intervention's effect

ARCHETYPES: tuple[Archetype, ...] = (
    Archetype("LOYAL",        0.25, 0.55,  1.0),   # mostly "sure things"
    Archetype("BUSY",         0.30, 0.20,  1.3),   # the persuadables
    Archetype("NEW",          0.20, 0.15,  1.0),
    Archetype("CHRONIC_FAIL", 0.15, 0.05,  0.3),   # mostly lost causes
    Archetype("SLEEPING_DOG", 0.10, 0.45, -0.8),   # contact makes it worse
)

# How well each intervention works on each failure class, before responsiveness.
BASE_EFFECT: dict[FailureClass, dict[ActionType, float]] = {
    FailureClass.INSUFFICIENT_FUNDS: {
        ActionType.RETRY: 0.18, ActionType.REMIND: 0.25,
        ActionType.PAY_LINK: 0.30, ActionType.METHOD_CHANGE: 0.20, ActionType.HUMAN: 0.35},
    FailureClass.CARD_EXPIRED: {
        ActionType.RETRY: 0.02, ActionType.REMIND: 0.10,
        ActionType.PAY_LINK: 0.22, ActionType.METHOD_CHANGE: 0.55, ActionType.HUMAN: 0.40},
    FailureClass.CARD_BLOCKED: {
        ActionType.RETRY: 0.01, ActionType.REMIND: 0.08,
        ActionType.PAY_LINK: 0.20, ActionType.METHOD_CHANGE: 0.48, ActionType.HUMAN: 0.38},
    FailureClass.ISSUER_DOWN: {
        ActionType.RETRY: 0.60, ActionType.REMIND: 0.15,
        ActionType.PAY_LINK: 0.25, ActionType.METHOD_CHANGE: 0.20, ActionType.HUMAN: 0.20},
    FailureClass.NETWORK_ERROR: {
        ActionType.RETRY: 0.45, ActionType.REMIND: 0.15,
        ActionType.PAY_LINK: 0.22, ActionType.METHOD_CHANGE: 0.15, ActionType.HUMAN: 0.18},
    FailureClass.AUTH_ABANDONED: {
        ActionType.RETRY: 0.10, ActionType.REMIND: 0.30,
        ActionType.PAY_LINK: 0.40, ActionType.METHOD_CHANGE: 0.15, ActionType.HUMAN: 0.30},
    FailureClass.MANDATE_INVALID: {
        ActionType.RETRY: 0.01, ActionType.REMIND: 0.12,
        ActionType.PAY_LINK: 0.25, ActionType.METHOD_CHANGE: 0.50, ActionType.HUMAN: 0.35},
    FailureClass.CHECKOUT_ABANDONED: {
        ActionType.RETRY: 0.05, ActionType.REMIND: 0.28,
        ActionType.PAY_LINK: 0.38, ActionType.METHOD_CHANGE: 0.10, ActionType.HUMAN: 0.25},
    FailureClass.UNKNOWN: {
        ActionType.RETRY: 0.12, ActionType.REMIND: 0.15,
        ActionType.PAY_LINK: 0.20, ActionType.METHOD_CHANGE: 0.15, ActionType.HUMAN: 0.20},
}

FAILURE_MIX = [
    (FailureClass.INSUFFICIENT_FUNDS, 0.28), (FailureClass.AUTH_ABANDONED, 0.20),
    (FailureClass.ISSUER_DOWN, 0.13), (FailureClass.CARD_EXPIRED, 0.11),
    (FailureClass.NETWORK_ERROR, 0.10), (FailureClass.CHECKOUT_ABANDONED, 0.08),
    (FailureClass.CARD_BLOCKED, 0.05), (FailureClass.MANDATE_INVALID, 0.03),
    (FailureClass.UNKNOWN, 0.02),
]

METHOD_MIX = [("upi", 0.45), ("card", 0.40), ("netbanking", 0.15)]

PRESETS: dict[str, dict] = {
    # the balanced world
    "default":        {"p_self_mult": 1.00, "retry_mult": 1.0, "jitter": 0.15},
    # organic recovery is high -> our edge over doing nothing shrinks
    "high_organic":   {"p_self_mult": 1.60, "retry_mult": 1.0, "jitter": 0.15},
    # dumb retries mostly work -> the fixed baseline should nearly tie us.
    # if we still win big here, the simulator is rigged. do not tune this away.
    #
    # ITEM 3a REDUCED THIS PRESET'S SCOPE AND IT IS KEPT ANYWAY. G8 now blocks
    # RETRY on every case with no mandate, so `retry_mult` only reaches the ~25%
    # that hold one and this world's first three columns now read identically to
    # `default`. It has largely stopped discriminating. Kept, and said out loud in
    # README.md, because a preset that lost its power and is documented is more
    # credible than one quietly deleted -- and because if a later item ever makes
    # retries reachable again, this is the world that will notice.
    "retry_friendly": {"p_self_mult": 1.00, "retry_mult": 2.0, "jitter": 0.15},
    # the same question retry_friendly used to ask, on a lever 3a cannot take
    # away. NOTE WHAT IT ACTUALLY MEASURES, WHICH IS NOT WHAT IT WAS ASKED FOR:
    # `policies.baseline_decide` only ever sends RETRY and REMIND, so doubling
    # PAY_LINK and METHOD_CHANGE cannot reach the baseline at all -- its column
    # is byte-identical to `default`. Only the ENGINE benefits, and our ceiling
    # share goes UP (80.7% -> 87.9%). So this is not an anti-rigging control; it
    # is the opposite, a world built out of our own best levers. Reported as
    # exactly that, and never as evidence the simulator is fair.
    "link_friendly":  {"p_self_mult": 1.00, "retry_mult": 1.0, "jitter": 0.15,
                       "link_mult": 2.0},
    # THIS is the honest successor to retry_friendly. After 3a the fixed
    # schedule's surviving lever is REMIND, so REMIND is what has to work
    # unusually well for "does the dumb tool nearly catch us" to mean anything.
    # It does the job: baseline climbs 37.7% -> 48.7% and our share of the
    # ceiling falls 80.7% -> 73.4%. A world that costs us is worth more than a
    # world that pays us.
    "remind_friendly": {"p_self_mult": 1.00, "retry_mult": 1.0, "jitter": 0.15,
                        "remind_mult": 2.0},
    # can we still learn when the signal is noisy?
    "noisy":          {"p_self_mult": 1.00, "retry_mult": 1.0, "jitter": 0.40},
}

# Which actions `link_mult` scales. Named rather than inlined so the preset table
# and this list cannot drift apart.
LINK_ACTIONS = (ActionType.PAY_LINK, ActionType.METHOD_CHANGE)


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------

@dataclass
class Customer:
    id: str
    archetype: str
    p_self: float
    responsiveness: float
    tenure_days: int
    past_failures: int
    past_recoveries: int
    opted_out: bool


@dataclass
class Obligation:
    id: str
    customer_id: str
    amount: int                 # paise
    kind: ObligationKind
    method: str
    is_mandate: bool
    failure_class: FailureClass
    opened_at: datetime
    risk_blocked: bool


@dataclass
class Truth:
    """NEVER visible to app/. Only sim/ may read this."""
    self_pay_at: datetime | None
    p_none: float                          # P(pays | we do nothing)
    p_act: dict[ActionType, float]         # P(pays | we take this action)
    reversed_later: bool

    def uplift(self, action: ActionType) -> float:
        return self.p_act.get(action, self.p_none) - self.p_none


@dataclass
class World:
    customers: dict[str, Customer]
    obligations: list[Obligation]
    truth: dict[str, Truth]
    downtimes: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    preset: str = "default"
    seed: int = 42

    def customer_of(self, ob: Obligation) -> Customer:
        return self.customers[ob.customer_id]

    def in_downtime(self, method: str, t: datetime):
        for m, a, b in self.downtimes:
            if m == method and a <= t < b:
                return b
        return None


def _pick(rng, options):
    labels = [o[0] for o in options]
    probs = np.array([o[1] for o in options], dtype=float)
    return labels[int(rng.choice(len(labels), p=probs / probs.sum()))]


def generate(n: int, seed: int = 42, preset: str = "default", window_hours: int = 168,
             reversal_rate: float = 0.03) -> World:
    rng = np.random.default_rng(seed)
    p = PRESETS[preset]

    customers: dict[str, Customer] = {}
    obligations: list[Obligation] = []
    truth: dict[str, Truth] = {}

    arch_probs = np.array([a.share for a in ARCHETYPES])
    arch_probs = arch_probs / arch_probs.sum()

    for i in range(n):
        cid = f"cust_{i}"
        arch = ARCHETYPES[int(rng.choice(len(ARCHETYPES), p=arch_probs))]
        p_self = float(np.clip(arch.p_self * p["p_self_mult"] * rng.normal(1.0, 0.12), 0.01, 0.92))
        customers[cid] = Customer(
            id=cid, archetype=arch.name, p_self=p_self,
            responsiveness=arch.responsiveness * float(rng.normal(1.0, 0.10)),
            tenure_days=int(rng.integers(1, 1200)),
            past_failures=int(rng.integers(0, 6)),
            past_recoveries=int(rng.integers(0, 4)),
            opted_out=bool(rng.random() < 0.03),
        )

        # log-normal amounts: many small, a few large. paise.
        amount = int(np.clip(rng.lognormal(mean=11.4, sigma=1.1), 5_000, 50_000_000))
        kind = _pick(rng, [(ObligationKind.ORDER, 0.5), (ObligationKind.SUBSCRIPTION, 0.35),
                           (ObligationKind.INVOICE, 0.1), (ObligationKind.CHECKOUT, 0.05)])
        method = _pick(rng, METHOD_MIX)
        fc = _pick(rng, FAILURE_MIX)
        if kind == ObligationKind.CHECKOUT:
            fc = FailureClass.CHECKOUT_ABANDONED

        ob = Obligation(
            id=f"ob_{i}", customer_id=cid, amount=amount, kind=kind, method=method,
            is_mandate=(kind == ObligationKind.SUBSCRIPTION),
            failure_class=fc,
            opened_at=T0 + timedelta(hours=float(rng.uniform(0, 48))),
            risk_blocked=bool(rng.random() < 0.02),
        )
        obligations.append(ob)

        c = customers[cid]
        self_pay_at = None
        if rng.random() < c.p_self:
            # most organic recovery is fast: they curse and retry within hours
            delay = float(np.clip(rng.lognormal(mean=1.6, sigma=1.2), 0.2, window_hours - 1))
            self_pay_at = ob.opened_at + timedelta(hours=delay)

        p_act: dict[ActionType, float] = {}
        for action, base in BASE_EFFECT[fc].items():
            eff = base * c.responsiveness
            if action == ActionType.RETRY:
                eff *= p["retry_mult"]
            # Guarded rather than an unconditional `*= p.get("link_mult", 1.0)`:
            # every preset that predates link_friendly must produce byte-identical
            # numbers, and not touching the value at all is a stronger guarantee
            # than trusting float identity on a multiply.
            if action in LINK_ACTIONS and "link_mult" in p:
                eff *= p["link_mult"]
            if action == ActionType.REMIND and "remind_mult" in p:
                eff *= p["remind_mult"]
            eff *= float(rng.normal(1.0, p["jitter"]))
            if eff >= 0:
                # a positive effect claims part of the remaining headroom
                p_act[action] = float(np.clip(c.p_self + eff * (1 - c.p_self), 0.005, 0.97))
            else:
                # a negative effect (sleeping dog) suppresses payment they'd have made
                p_act[action] = float(np.clip(c.p_self * (1 + eff), 0.005, 0.97))

        truth[ob.id] = Truth(
            self_pay_at=self_pay_at, p_none=c.p_self, p_act=p_act,
            reversed_later=bool(rng.random() < reversal_rate),
        )

    # a handful of issuer outages, so the downtime gate has something to catch
    downtimes = []
    for _ in range(6):
        m = _pick(rng, METHOD_MIX)
        start = T0 + timedelta(hours=float(rng.uniform(0, window_hours - 6)))
        downtimes.append((m, start, start + timedelta(hours=float(rng.uniform(1, 5)))))

    return World(customers=customers, obligations=obligations, truth=truth,
                 downtimes=downtimes, preset=preset, seed=seed)
