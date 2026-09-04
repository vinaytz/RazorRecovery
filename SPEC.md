# RazorRecovery — SPEC

Technical contract. `TASKS.md` says when to build each part; this says what it is.

---

## 1. Vocabulary

**Obligation** — the debt. An order, invoice, or one subscription cycle. This is the
unit, *not* the payment. One obligation has many payment attempts with different
`payment_id`s. A case closes when the obligation settles, regardless of which
attempt did it or whether we caused it.

**Case** — one recovery attempt-lifecycle over one obligation, under one arm.

**Rung** — position on the escalation ladder. Cases climb; they never descend or skip.

**Arm** — `CONTROL` | `BASELINE` | `ENGINE` | `ORACLE`. Same cases, four policies.

**Snapshot** — frozen "what we knew at time T". The only input to a decision.

---

## 2. Layout

```
razorrecovery/
  app/
    api/          webhooks.py  dashboard.py  replay.py  chaos.py
    controllers/  ingest.py  decide.py  execute.py
    domain/       models.py gates.py ladder.py scoring.py timing.py
                  allocator.py engine.py            ← PURE
    services/     clock.py executor.py notifier.py llm.py bandit.py
    repos/        db.py event_repo.py case_repo.py decision_repo.py
                  action_repo.py contact_repo.py posterior_repo.py
  sim/            world.py  runner.py
  web/            index.html
  config/         default.yaml
  tests/          test_purity.py test_gates.py test_idempotency.py
  main.py  run_benchmark.py  README.md  requirements.txt
```

---

## 3. Enums

```python
class FailureClass(str, Enum):
    INSUFFICIENT_FUNDS  = "INSUFFICIENT_FUNDS"
    CARD_EXPIRED        = "CARD_EXPIRED"
    CARD_BLOCKED        = "CARD_BLOCKED"
    ISSUER_DOWN         = "ISSUER_DOWN"
    NETWORK_ERROR       = "NETWORK_ERROR"
    AUTH_ABANDONED      = "AUTH_ABANDONED"   # user dropped at OTP / UPI collect
    MANDATE_INVALID     = "MANDATE_INVALID"
    CHECKOUT_ABANDONED  = "CHECKOUT_ABANDONED"
    UNKNOWN             = "UNKNOWN"

class ActionType(str, Enum):
    WAIT = "WAIT"; RETRY = "RETRY"; REMIND = "REMIND"; PAY_LINK = "PAY_LINK"
    METHOD_CHANGE = "METHOD_CHANGE"; HUMAN = "HUMAN"; WRITE_OFF = "WRITE_OFF"
    NONE = "NONE"                    # the counterfactual. used by the bandit only.

class StopReason(str, Enum):
    CONTROL_ARM = "CONTROL_ARM"; ALREADY_SETTLED = "ALREADY_SETTLED"
    PROMISED = "PROMISED"; NON_RETRYABLE = "NON_RETRYABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"; WINDOW_EXPIRED = "WINDOW_EXPIRED"
    OPTED_OUT = "OPTED_OUT"; QUIET_HOURS = "QUIET_HOURS"
    MANDATE_NOTICE_REQUIRED = "MANDATE_NOTICE_REQUIRED"; AFA_REQUIRED = "AFA_REQUIRED"
    DUPLICATE_PENDING = "DUPLICATE_PENDING"; RISK_BLOCK = "RISK_BLOCK"
    DOWNTIME = "DOWNTIME"; EV_NEGATIVE = "EV_NEGATIVE"
    LADDER_TOP = "LADDER_TOP"; CONTACT_BUDGET = "CONTACT_BUDGET"

class Arm(str, Enum):
    CONTROL="CONTROL"; BASELINE="BASELINE"; ENGINE="ENGINE"; ORACLE="ORACLE"
```

`ActionType.NONE` is not a no-op — it is the do-nothing counterfactual the bandit
scores against. Uplift is meaningless without it.

---

## 4. `CaseSnapshot` — the only thing a decision may see

```python
@dataclass(frozen=True)
class CaseSnapshot:
    case_id: str
    obligation_id: str
    customer_id: str
    merchant_id: str
    arm: Arm

    amount_due: int              # paise
    amount_settled: int
    currency: str = "INR"
    kind: str                    # ORDER | INVOICE | SUBSCRIPTION | CHECKOUT

    failure_class: FailureClass
    method: str                  # card | upi | netbanking
    is_mandate: bool

    now: datetime                # injected. never call the clock inside domain/.
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
    last_notice_sent_at: datetime | None   # for the 24h pre-debit rule
    afa_valid: bool

    obligation_settled: bool     # refreshed at execute time, not just decide time
    method_in_downtime: bool
    downtime_ends_at: datetime | None
    pending_action_types: tuple[ActionType, ...]

    @property
    def amount_remaining(self) -> int: return self.amount_due - self.amount_settled

    @property
    def segment(self) -> str:
        return f"{self.failure_class}|{amount_band(self.amount_remaining)}|{min(self.attempts,3)}|{self.method}"
```

`amount_band`: `<50000` / `<200000` / `<1000000` / `>=1000000` paise → `S/M/L/XL`.

Nothing else. If the engine needs a fact, it goes in this struct — never fetched.

---

## 5. Gates — ordered, first match wins

`run_gates(snap, config) -> GateResult(blocked: bool, reason: StopReason|None, blocked_actions: set, wait_until: datetime|None, trace: list[str])`

```
G0  HOLDOUT           snap.arm == CONTROL  →  STOP(CONTROL_ARM)
G1  SETTLED           obligation_settled or amount_remaining <= 0 → STOP(ALREADY_SETTLED)
G2  PROMISED          now < promised_until → WAIT(promised_until)
G3  RISK              risk_blocked → STOP(RISK_BLOCK)
G4  OPTED_OUT         opted_out → STOP(OPTED_OUT)
G5  WINDOW            now > opened_at + recovery_window → STOP(WINDOW_EXPIRED) + WRITE_OFF
G6  BUDGET            attempts >= max_attempts → block RETRY
G7  LADDER_TOP        rung >= max_rung → STOP(LADDER_TOP) + WRITE_OFF
G8  NON_RETRYABLE     failure_class in {CARD_EXPIRED, CARD_BLOCKED, MANDATE_INVALID}
                         → block RETRY  (METHOD_CHANGE stays legal)
G9  DOWNTIME          method_in_downtime → block RETRY, WAIT(downtime_ends_at)
G10 MANDATE_NOTICE    is_mandate and RETRY and (last_notice_sent_at is None
                         or now - last_notice_sent_at < 24h)
                         → block RETRY, reason MANDATE_NOTICE_REQUIRED
G11 AFA               is_mandate and amount_remaining > afa_limit and not afa_valid
                         → block RETRY, reason AFA_REQUIRED (METHOD_CHANGE legal)
G12 QUIET_HOURS       now outside contact_window → block REMIND/PAY_LINK/METHOD_CHANGE,
                         WAIT(next window open)
G13 CONTACT_CAP       contacts_last_7d >= max_contacts_7d → block all contact actions
G14 DUPLICATE         action type already in pending_action_types → block that type
```

G0 runs first, always. That single ordering is what makes the control arm honest.

`trace` must record every gate evaluated and its verdict — this is the audit trail.

---

## 6. Ladder

```
0 WAIT → 1 RETRY → 2 REMIND → 3 PAY_LINK → 4 METHOD_CHANGE → 5 HUMAN → 6 WRITE_OFF
```

`legal_next_rungs(snap, gate_result, config) -> list[ActionType]`
Returns the action at `rung+1` plus `WAIT`, minus anything the gates blocked.
Climb one at a time. Never skip, never descend. `max_rung` from config.

---

## 7. Scoring

```python
def score(snap, action, posterior, rng) -> tuple[float, dict]:
    p_act  = posterior.sample(snap.segment, action, rng)
    p_none = posterior.sample(snap.segment, ActionType.NONE, rng)
    uplift = p_act - p_none                        # ← may be negative. that's the point.
    ev = uplift * snap.amount_remaining \
         - config.action_cost[action] \
         - config.friction_cost[action] * friction_multiplier(snap)
    return ev, {"p_act": p_act, "p_none": p_none, "uplift": uplift, "ev": ev}
```

`friction_multiplier` rises with `contacts_last_7d`. A customer already contacted
three times this week is expensive to contact again.

If `max(ev) <= 0` → `STOP(EV_NEGATIVE)`. **Doing nothing is a valid, scored outcome.**

**Posterior** (`services/bandit.py`): `Beta(alpha, beta)` per `(segment, action)`,
including `action = NONE`. `sample()` is a Thompson draw. Empirical-Bayes shrinkage:
if `alpha+beta < 30`, blend toward the parent segment (drop `attempts` from the key,
then drop `method`). Update on outcome: success → `alpha += 1`, else `beta += 1`.

---

## 8. Allocator

```python
def allocate(proposals, config) -> list[Decision]
```
Per batch, not per case. Sort by `ev / max(1, contacts_used(action))` descending,
spend `global_contact_budget`, everything below the line becomes
`WAIT` with `StopReason.CONTACT_BUDGET`. Recovery is a knapsack, not N independent
decisions.

---

## 9. The two swap points

Same engine, two worlds. Only these differ:

| Port | Real | Sim |
|---|---|---|
| `Clock` | wall clock | `VirtualClock` — jumps, no sleeping |
| `Executor` | `RazorpayExecutor` | `SimExecutor` — consults `HiddenTruth` |

`Notifier` and `LLM` follow the same pattern (`CounterNotifier`, `StubLLM`).
Nothing else changes between benchmark and live. Say this in the README.

---

## 10. Simulator — `sim/world.py`

### Latent traits (engine NEVER sees these)

Per customer, drawn from an archetype:

| Archetype | share | `p_self` | `responsiveness` | note |
|---|---|---|---|---|
| `LOYAL` | 25% | 0.55 | 1.0 | usually pays anyway |
| `BUSY` | 30% | 0.20 | 1.3 | the persuadables 🎯 |
| `NEW` | 20% | 0.15 | 1.0 | |
| `CHRONIC_FAIL` | 15% | 0.05 | 0.3 | mostly lost causes |
| `SLEEPING_DOG` | 10% | 0.45 | **−0.8** | contact makes it worse |

### Hidden truth per case

```python
self_pay_at: datetime | None    # drawn from p_self; lognormal delay in window
true_p[action] = clip(base_effect[failure_class][action] * responsiveness, 0, 0.95)
```

`base_effect` matrix (the agent fills in the rest sensibly, these anchor it):

| failure_class | RETRY | REMIND | PAY_LINK | METHOD_CHANGE |
|---|---|---|---|---|
| `INSUFFICIENT_FUNDS` | 0.18 | 0.25 | 0.30 | 0.20 |
| `CARD_EXPIRED` | 0.02 | 0.10 | 0.22 | **0.55** |
| `ISSUER_DOWN` | **0.60** | 0.15 | 0.25 | 0.20 |
| `AUTH_ABANDONED` | 0.10 | 0.30 | **0.40** | 0.15 |
| `CHECKOUT_ABANDONED` | 0.05 | 0.28 | **0.38** | 0.10 |

For `SLEEPING_DOG`, contact actions get a negative multiplier, so `true_p[REMIND] <
true_p[NONE]`. That is a real negative uplift and the engine must learn to avoid it
**from outcomes**, never by being told.

### Resolution

```
when an action executes at time t:
    if self_pay_at is not None and self_pay_at <= t:
        → obligation already settled. action ABORTED. case = SELF_RECOVERED.
          if a contact was sent → count a FALSE CHASE.       ← this metric matters
    else:
        roll rng < true_p[action] → RECOVERED (attributed to us)
at window end:
    self_pay_at within window and nothing else recovered → SELF_RECOVERED
    otherwise → WRITTEN_OFF
reversals: at T+30, reverse `reversal_rate` of recoveries → net vs gross
```

### World presets — run all of them

```
default          the balanced world
high_organic     p_self × 1.6   → control recovers a lot, our edge shrinks
retry_friendly   RETRY effects × 2 → the dumb baseline should nearly tie us
noisy            true_p jittered ±40% → can we still learn?
```

**Report all four in the README, including the ones where we barely win.** This is
the single most credible thing in the submission. Do not skip it.

---

## 11. Arms

| Arm | Policy |
|---|---|
| `CONTROL` | G0 stops everything. Never acts. Measures organic recovery. |
| `BASELINE` | fixed: RETRY at T+0, T+1h, T+24h, then one REMIND, then stop. Ignores context. |
| `ENGINE` | gates → ladder → uplift → allocator. |
| `ORACLE` | reads `HiddenTruth`. Acts only where `true_p[best] > p_self_by_window`, picks argmax. Upper bound only — never compared as a peer. |

Same seed, same cases, four policies. Oracle turns "we recovered 46%" into
"we captured 73% of what was actually winnable", which is a number that survives scrutiny.

---

## 12. Metrics — `run_benchmark.py` emits `results.json`

```
per arm:  cases, at_risk, gross_recovered, net_recovered, recovery_rate,
          contacts_sent, actions_taken, written_off_count, written_off_value

headline: incremental = engine.gross - control.gross
          incremental_ci = bootstrap 95% (1000 resamples)
          pct_of_oracle = (engine - control) / (oracle - control)

honesty:  false_chase_per_10k     contacts to already-settled obligations
          double_charge_count     must be 0
          sleeping_dogs_avoided   count + ₹ value deliberately not chased
          where_we_lost           segments where engine recovery < control recovery
```

`false_chase_per_10k` and `where_we_lost` are the two metrics nobody else will have.
Put them on the dashboard, not just in the JSON.

---

## 13. Schema (SQLite, SQLAlchemy Core)

```sql
obligations(id TEXT PK, merchant_id, customer_id, amount_due INT, amount_settled INT,
            kind, status, opened_at, settled_at)
events(id INTEGER PK, dedupe_key TEXT UNIQUE, obligation_id, type, payload JSON, received_at)
cases(id TEXT PK, obligation_id, run_id, arm, rung INT, attempts INT, status,
      promised_until, wake_at, opened_at, closed_at)
decisions(id TEXT PK, case_id, decided_at, snapshot JSON, gate_trace JSON,
          chosen_action, candidate_scores JSON, stop_reason, config_version, code_version)
actions(id TEXT PK, decision_id, case_id, type, execute_at, status,
        idem_key TEXT UNIQUE, attempts INT)
outcomes(id INTEGER PK, action_id, result, detail, observed_at)
contacts(id INTEGER PK, customer_id, channel, sent_at)
posteriors(segment TEXT, action TEXT, alpha REAL, beta REAL, PRIMARY KEY(segment, action))
```

`UNIQUE` on `events.dedupe_key` and `actions.idem_key` is the entire duplicate
defence. `INSERT ... ON CONFLICT DO NOTHING`. Two lines, one demo.

---

## 14. Execution — `controllers/execute.py`

```python
def run(action):
    fresh = executor.fetch_obligation(action.obligation_id)   # ALWAYS. first line.
    if fresh.settled:
        abort(action, "ALREADY_SETTLED"); close_case(SELF_RECOVERED); return
    mark(action, IN_FLIGHT)                                    # before the call
    try:
        result = executor.execute(action, idem_key=action.idem_key)
    except Timeout:
        mark(action, UNKNOWN); return                          # never guess
    record_outcome(result)
    if action.is_contact: contact_repo.insert(...)
    bandit.update(segment, action.type, success=result.ok)
```

The decision is made in advance; **permission is checked at the last second.** You
can abort a retry, but you cannot unsend an SMS — so contact actions get the
strictest re-check and the shortest decide→execute gap.

---

## 15. API

```
POST /webhooks/razorpay        verify sig on RAW body → insert event → 200. <10ms. no logic.
GET  /api/scoreboard?run_id=   the four arms + all metrics from §12
GET  /api/cases?run_id=&arm=   paginated list
GET  /api/case/{id}            snapshot, gate trace, candidate scores, outcome
GET  /api/replay/{decision_id} re-run decide() on the stored snapshot → {then, now, match}
POST /api/chaos/{kind}         duplicate_webhook | kill_llm | executor_timeout | pay_midflight
GET  /                         serves web/index.html
```

Replay is twelve lines and it is your best demo:
```python
d = decision_repo.get(id)
out = engine.decide(CaseSnapshot(**d.snapshot), config.version(d.config_version), d.posterior_snapshot)
return {"then": d.chosen_action, "now": out.action, "match": d.chosen_action == out.action}
```
It only works because `decide()` never touched a database. Protect that.

---

## 16. Dashboard — `web/index.html`

One file. Chart.js from CDN. No build step. Three views, tab-switched.

**Scoreboard** — spend all boldness here and nowhere else. The four-bar comparison
is large and is the first thing on screen. `CONTROL` is visually distinct from the
other three (it is the argument, not a competitor). Label the control→engine gap
directly on the chart with the incremental ₹ and its interval. Below: the honesty
metrics from §12, including `where_we_lost`.

**Cases** — table: id, amount, failure class, arm, rung, status, outcome. Filterable by arm.

**Decision card** — one case: what we knew, gates evaluated with verdicts, every
candidate with `p_act / p_none / uplift / ev`, what was chosen, what happened, and a
**Replay** button that shows `match: true`.

Style: audience is payments-ops engineers; the job is "is this number trustworthy",
not "is this pretty". One sans family, two weights, tabular figures for all numbers,
generous whitespace, sentence case. No gradient washes, no identical rounded cards,
no all-caps eyebrow labels, no icon set, no entrance animations. Empty states say
what to do next.

---

## 17. LLM — `services/llm.py`

`StubLLM` is the default and is written **first**. A dead API key must never block you.

| # | Job | Contract | Notes |
|---|---|---|---|
| ① | error string → `FailureClass` | returns enum member, else `UNKNOWN` | cached on `sha1(text)`. ~40 unique strings, so ~40 calls not 5,000. |
| ③ | message | **selects** a DLT-approved template id + fills `{{slots}}` | freeform only for email/in-app. Never freeform on SMS/WhatsApp. |
| ④ | narration | decision trace → one plain-English paragraph | read-only, post-hoc, for the decision card. |

**PII never leaves the process.** The model sees `{{NAME}}`, `{{AMOUNT}}`,
`{{MERCHANT}}`; you hydrate locally after the response returns.

Gemini Flash, JSON mode, `temperature=0` for ① and ④.

---

## 18. Razorpay test mode

`RAZORPAY_WEBHOOK_SECRET` is **not** `RAZORPAY_KEY_SECRET`. Verify the signature on
the **raw request body**, before JSON parsing.

Subscribe to failures *and* successes: `payment.failed`, `payment.captured`,
`order.paid`, `subscription.charged`, `subscription.halted`,
`payment.downtime.started`, `payment.downtime.resolved`.

Success events close cases as `SELF_RECOVERED` and cancel pending actions. A
recovery engine that only listens for failures is the classic bug — it chases people
who already paid.

Capture 3–5 real webhook payloads to `fixtures/webhooks/*.json` early. If the tunnel
fails on demo day, replay the fixtures through the same endpoint and say so honestly.
