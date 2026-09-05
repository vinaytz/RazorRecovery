"""
Pure domain models. No IO, no clock, no randomness.

CaseSnapshot is the contract: it is the ONLY thing a decision may see. If the
engine needs a fact, it goes in this struct. It is never fetched mid-decision.
That single constraint is what makes replay, the audit trail, and the 5,000-case
benchmark all work.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class FailureClass(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    CARD_EXPIRED = "CARD_EXPIRED"
    CARD_BLOCKED = "CARD_BLOCKED"
    ISSUER_DOWN = "ISSUER_DOWN"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_ABANDONED = "AUTH_ABANDONED"
    MANDATE_INVALID = "MANDATE_INVALID"
    CHECKOUT_ABANDONED = "CHECKOUT_ABANDONED"
    UNKNOWN = "UNKNOWN"


class ActionType(str, Enum):
    WAIT = "WAIT"
    RETRY = "RETRY"
    REMIND = "REMIND"
    PAY_LINK = "PAY_LINK"
    METHOD_CHANGE = "METHOD_CHANGE"
    HUMAN = "HUMAN"
    WRITE_OFF = "WRITE_OFF"
    NONE = "NONE"  # the do-nothing counterfactual. bandit only. never executed.


class StopReason(str, Enum):
    CONTROL_ARM = "CONTROL_ARM"
    ALREADY_SETTLED = "ALREADY_SETTLED"
    PROMISED = "PROMISED"
    NON_RETRYABLE = "NON_RETRYABLE"
    NO_MANDATE_TO_RETRY = "NO_MANDATE_TO_RETRY"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    OPTED_OUT = "OPTED_OUT"
    QUIET_HOURS = "QUIET_HOURS"
    MANDATE_NOTICE_REQUIRED = "MANDATE_NOTICE_REQUIRED"
    AFA_REQUIRED = "AFA_REQUIRED"
    DUPLICATE_PENDING = "DUPLICATE_PENDING"
    RISK_BLOCK = "RISK_BLOCK"
    DOWNTIME = "DOWNTIME"
    EV_NEGATIVE = "EV_NEGATIVE"
    LADDER_TOP = "LADDER_TOP"
    CONTACT_BUDGET = "CONTACT_BUDGET"
    NO_LEGAL_ACTION = "NO_LEGAL_ACTION"


class Arm(str, Enum):
    CONTROL = "CONTROL"
    BASELINE = "BASELINE"
    ENGINE = "ENGINE"
    ORACLE = "ORACLE"


class ObligationKind(str, Enum):
    ORDER = "ORDER"
    INVOICE = "INVOICE"
    SUBSCRIPTION = "SUBSCRIPTION"
    CHECKOUT = "CHECKOUT"


# The escalation ladder. Index == rung. Climb one at a time, never descend.
RUNG_ORDER: tuple[ActionType, ...] = (
    ActionType.WAIT,
    ActionType.RETRY,
    ActionType.REMIND,
    ActionType.PAY_LINK,
    ActionType.METHOD_CHANGE,
    ActionType.HUMAN,
    ActionType.WRITE_OFF,
)

CONTACT_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.REMIND,
    ActionType.PAY_LINK,
    ActionType.METHOD_CHANGE,
    ActionType.HUMAN,
})

# Instruments that cannot be fixed by hitting retry again.
NON_RETRYABLE_FAILURES: frozenset[FailureClass] = frozenset({
    FailureClass.CARD_EXPIRED,
    FailureClass.CARD_BLOCKED,
    FailureClass.MANDATE_INVALID,
})

ESSENTIAL_KINDS: frozenset[ObligationKind] = frozenset({ObligationKind.SUBSCRIPTION})


def amount_band(paise: int) -> str:
    if paise < 50_000:
        return "S"
    if paise < 200_000:
        return "M"
    if paise < 1_000_000:
        return "L"
    return "XL"


def rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


# --------------------------------------------------------------------------
# Config (pure data. the loader lives in app/config_loader.py)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    version: str

    holdout_pct: int
    seed: int
    reversal_rate: float
    attribution_window_days: int

    window_hours: int
    max_attempts: int
    max_rung: int
    max_contacts_7d: int

    afa_limit: int
    afa_limit_essentials: int
    mandate_pre_debit_notice_hours: int
    quiet_start: int
    quiet_end: int
    respect_dnd: bool

    global_contact_budget_per_tick: int

    action_cost: dict[str, int]
    friction_cost: dict[str, int]
    contacts_used: dict[str, int]

    prior_alpha: float
    prior_beta: float
    shrinkage_threshold: int
    bandit_decay: float

    baseline_retry_schedule_hours: tuple[int, ...]

    payday_window_days: tuple[int, ...]
    insufficient_funds_wait_for_payday: bool
    downtime_backoff_minutes: int

    dry_run: bool
    batch_size: int

    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def cost_of(self, action: ActionType) -> int:
        return int(self.action_cost.get(action.value, 0))

    def friction_of(self, action: ActionType) -> int:
        return int(self.friction_cost.get(action.value, 0))

    def contacts_for(self, action: ActionType) -> int:
        return int(self.contacts_used.get(action.value, 0))

    def afa_limit_for(self, kind: ObligationKind) -> int:
        return self.afa_limit_essentials if kind in ESSENTIAL_KINDS else self.afa_limit


# --------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseSnapshot:
    """Everything the engine is allowed to know, frozen at time `now`.

    Anything not on this struct does not exist as far as a decision is concerned.
    Never add a field sourced from simulator ground truth.
    """
    case_id: str
    obligation_id: str
    customer_id: str
    merchant_id: str
    arm: Arm

    amount_due: int          # paise
    amount_settled: int
    kind: ObligationKind
    currency: str

    failure_class: FailureClass
    method: str              # card | upi | netbanking
    is_mandate: bool

    now: datetime
    opened_at: datetime
    attempts: int
    rung: int
    last_action_at: datetime | None
    promised_until: datetime | None

    customer_tenure_days: int
    customer_past_failures: int
    customer_past_recoveries: int
    contacts_last_7d: int
    opted_out: bool
    risk_blocked: bool
    last_notice_sent_at: datetime | None
    afa_valid: bool

    obligation_settled: bool
    method_in_downtime: bool
    downtime_ends_at: datetime | None
    pending_action_types: tuple[ActionType, ...] = ()

    @property
    def amount_remaining(self) -> int:
        return max(0, self.amount_due - self.amount_settled)

    @property
    def segment(self) -> str:
        return "|".join([
            self.failure_class.value,
            amount_band(self.amount_remaining),
            str(min(self.attempts, 3)),
            self.method,
        ])

    @property
    def age_hours(self) -> float:
        return (self.now - self.opened_at).total_seconds() / 3600.0

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if isinstance(v, datetime):
                out[k] = v.isoformat()
            elif isinstance(v, Enum):
                out[k] = v.value
            elif isinstance(v, tuple):
                out[k] = [x.value if isinstance(x, Enum) else x for x in v]
            else:
                out[k] = v
        return out

    @staticmethod
    def from_json(d: dict[str, Any]) -> "CaseSnapshot":
        def dt(x): return datetime.fromisoformat(x) if x else None
        return CaseSnapshot(
            case_id=d["case_id"], obligation_id=d["obligation_id"],
            customer_id=d["customer_id"], merchant_id=d["merchant_id"],
            arm=Arm(d["arm"]),
            amount_due=d["amount_due"], amount_settled=d["amount_settled"],
            kind=ObligationKind(d["kind"]), currency=d["currency"],
            failure_class=FailureClass(d["failure_class"]),
            method=d["method"], is_mandate=d["is_mandate"],
            now=dt(d["now"]), opened_at=dt(d["opened_at"]),
            attempts=d["attempts"], rung=d["rung"],
            last_action_at=dt(d.get("last_action_at")),
            promised_until=dt(d.get("promised_until")),
            customer_tenure_days=d["customer_tenure_days"],
            customer_past_failures=d["customer_past_failures"],
            customer_past_recoveries=d["customer_past_recoveries"],
            contacts_last_7d=d["contacts_last_7d"],
            opted_out=d["opted_out"], risk_blocked=d["risk_blocked"],
            last_notice_sent_at=dt(d.get("last_notice_sent_at")),
            afa_valid=d["afa_valid"],
            obligation_settled=d["obligation_settled"],
            method_in_downtime=d["method_in_downtime"],
            downtime_ends_at=dt(d.get("downtime_ends_at")),
            pending_action_types=tuple(ActionType(x) for x in d.get("pending_action_types", [])),
        )


# --------------------------------------------------------------------------
# Gate + decision results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GateTrace:
    gate: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class GateResult:
    """`blocked` stops the case entirely. `blocked_actions` only removes options."""
    blocked: bool = False
    stop_reason: StopReason | None = None
    terminal_action: ActionType | None = None   # WRITE_OFF when the case is dead
    blocked_actions: frozenset[ActionType] = frozenset()
    wait_until: datetime | None = None
    wait_reason: StopReason | None = None
    trace: tuple[GateTrace, ...] = ()

    def trace_json(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self.trace]


@dataclass(frozen=True)
class ScoredAction:
    action: ActionType
    ev: float
    p_act: float
    p_none: float
    uplift: float
    cost: int
    friction: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value, "ev": round(self.ev, 2),
            "p_act": round(self.p_act, 4), "p_none": round(self.p_none, 4),
            "uplift": round(self.uplift, 4),
            "cost": self.cost, "friction": round(self.friction, 2),
        }


@dataclass(frozen=True)
class Decision:
    case_id: str
    action: ActionType
    execute_at: datetime | None
    stop_reason: StopReason | None
    gate_trace: tuple[GateTrace, ...]
    candidates: tuple[ScoredAction, ...]
    snapshot: CaseSnapshot
    decided_at: datetime
    config_version: str
    notes: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.action in (ActionType.WRITE_OFF,) or (
            self.action == ActionType.NONE and self.stop_reason is not None
        )

    @property
    def is_contact(self) -> bool:
        return self.action in CONTACT_ACTIONS

    def downgraded_to_wait(self, reason: StopReason, until: datetime | None) -> "Decision":
        return replace(self, action=ActionType.WAIT, stop_reason=reason, execute_at=until)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "action": self.action.value,
            "execute_at": self.execute_at.isoformat() if self.execute_at else None,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "gate_trace": [t.as_dict() for t in self.gate_trace],
            "candidates": [c.as_dict() for c in self.candidates],
            "decided_at": self.decided_at.isoformat(),
            "config_version": self.config_version,
            "notes": self.notes,
        }
