# HANDOFF — read this first

## Already built, tested, and RUNNING. Do not rewrite.

```
app/api/dashboard.py       runs, scoreboard, cases, case detail, highlights
app/api/replay.py          re-runs a stored decision, proves it matches
app/api/chaos.py           the four failure buttons
app/controllers/execute.py re-check, idempotency, UNKNOWN-on-timeout
app/workers/reconciler.py  resolves UNKNOWN
app/services/executor.py   SandboxExecutor (+ RazorpayExecutor stub for P7)
app/repos/store.py         SQLite: runs, cases, decisions, events, actions
sim/recorder.py            captures decisions + auto-finds demo highlights
web/index.html             the dashboard, 4 tabs, no build step
app/domain/models.py       enums, CaseSnapshot, Config, Decision, GateResult
app/domain/gates.py        G0..G14 ordered. G0 = holdout, first, always.
app/domain/ladder.py       escalation ladder
app/domain/scoring.py      uplift-based EV
app/domain/timing.py       downtime / payday / mandate-notice / quiet-hours
app/domain/allocator.py    batch contact-budget knapsack
app/domain/engine.py       decide() -- the pure core
app/domain/policies.py     control_decide, baseline_decide
app/services/bandit.py     Beta posteriors, Thompson, EB shrinkage
app/services/clock.py      RealClock | VirtualClock
app/config_loader.py       yaml -> frozen Config
app/metrics.py             bootstrap CI, Wilson, scoreboard, where-we-lost
sim/world.py               archetypes, HiddenTruth, 4 presets
sim/runner.py              four-arm harness on a virtual clock
run_benchmark.py           the experiment
main.py                    FastAPI entrypoint
config/default.yaml        every tunable, already chosen
tests/                     11 passing tests
```

**TASKS.md P0 through P6 are DONE.** Start at P7.

### Verify before you touch anything

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -q                                  # 15 passed
PYTHONPATH=. python run_benchmark.py --n 2000 --preset default
PYTHONPATH=. python main.py                                    # localhost:8000
```

### The headline number

**Incremental: Rs 947,291 (mean of 5 seeds, range Rs 855k – Rs 1,013k)**

Quote this, not a single seed. A judge who reruns with a different seed lands
inside that range, which is the point of stating it. The single-seed run below is
the reproducible reference, not the claim.

Expected (seed 42, n=2000). Byte-identical on every run — verified three times:

```
  arm              recovered     rate   contacts   actions   written off
  CONTROL         Rs 892,622    26.9%          0         0         1,423
  BASELINE      Rs 1,539,397    46.4%      1,296     3,532         1,051
  ENGINE        Rs 1,895,364    57.2%      2,995     3,844           912
  ORACLE        Rs 2,438,891    73.6%      2,581     4,865           545

  at risk            Rs 3,314,887
  organic (control)  Rs 892,622   <- money that arrived anyway
  INCREMENTAL        Rs 1,002,742
  net of reversals   Rs 986,127
    case-level CI      [Rs 819,143 .. Rs 1,209,087]   (bootstrap within this run)
  % of oracle ceiling   64.8%

  false chase /10k   engine 0.0   baseline 90.0
  left alone         1,044 cases  (Rs 1,215,971 deliberately not chased)
  written off        912 cases  (Rs 1,419,523)
```

### Two uncertainty numbers, not one

`--seeds 42,43,44,45,46` reruns the whole four-arm benchmark per seed (n=2000):

```
  seed         incremental   % of ceiling
  42          Rs 1,002,742          64.8%
  43            Rs 855,177          67.4%
  44          Rs 1,013,264          65.2%
  45            Rs 958,181          65.1%
  46            Rs 907,094          71.4%

  mean incremental      Rs 947,291
  seed-to-seed range    [Rs 855,177 .. Rs 1,013,264]   <- across 5 independent runs
  case-level CI (mean)  [Rs 724,144 .. Rs 1,180,281]   <- bootstrap within one run
  % of oracle ceiling   66.8%   [64.8% .. 71.4%]
```

The bootstrap CI resamples cases inside one run, so it only sees case-level
variance. The seed sweep redraws the world and every action roll. **Quote both.**
Headline the mean (Rs 947,291), not seed 42's Rs 1,002,742 — one seed is one draw.

Note the seed range (Rs 158k wide) came out *narrower* than the case-level CI
(Rs 456k wide). Five seeds is a small sample for a range and the bootstrap is
genuinely wide at n=2000, so read them as complementary, not one superseding
the other.

### Why "% of oracle ceiling" moved 71.8% → 64.8%

Not a regression. ORACLE now draws from a stable stream and recovers more (73.6%
vs the old 69.9%), so the ceiling — the denominator — grew. The engine is
unchanged. The old 71.8% was computed against an unreproducible oracle draw.

If CONTROL ever shows contacts > 0 or actions > 0, stop everything: gate G0 is
broken and the headline number is fiction.

### All four worlds (`--all-presets`, n=1500) — put this table in the README

| preset | control | baseline | engine | oracle | incremental | % of ceiling |
|---|---|---|---|---|---|---|
| default | 25.9% | 44.3% | 55.4% | 74.5% | Rs 741,674 | 60.7% |
| high_organic | 43.7% | 57.3% | 64.1% | 78.8% | Rs 487,838 | 58.3% |
| retry_friendly | 25.9% | 49.4% | 57.9% | 78.1% | Rs 803,102 | 61.3% |
| noisy | 25.9% | 45.6% | 55.7% | 73.0% | Rs 748,876 | 63.2% |

`high_organic` cuts our edge by a third (Rs 487,838 vs Rs 741,674) and is the only
world where control alone recovers 43.7%. `retry_friendly` lifts the baseline 5
points, exactly as intended — when retries work, the dumb tool catches up.
**Report all four. Do not tune them away** — they are the evidence the simulator
is not rigged, and that is worth more than a bigger number.

## The dashboard (already working)

`PYTHONPATH=. python main.py` -> http://localhost:8000

- **Scoreboard** — four bars, incremental headline with CI, % of oracle ceiling,
  false-chase rate, "left alone on purpose", write-offs, and a "where we lost" table
- **Cases** — filter by arm, or jump straight to an auto-found highlight
  (`sleeping_dog`, `gate_stop`, `write_off`, `big_save`)
- **Decision** — what we knew, every gate with its verdict, every candidate with
  `p_act / p_none / uplift / EV`, the choice, and **Replay** showing `match: true`
- **Live ops** — the four chaos buttons, each returning a verdict line

Verified end to end: replay on a sleeping-dog case returns
`REMIND uplift -0.037, EV -Rs 161 -> WAIT`, and matches.

## What YOU build

```
P7  DONE — see "P7: the live webhook path" below
P8  app/services/llm.py — StubLLM FIRST, then GeminiLLM. Wire LLM ①③④ per SPEC §17.
P9  README.md + the 5-minute video   ← 90 min, frozen, no code. Set an alarm.
```

## P7: the live webhook path — DONE, fixture-driven

```
app/api/webhooks.py          POST /webhooks/razorpay + /replay + /fixtures
app/controllers/ingest.py    classify, obligation identity, open/close case
app/services/executor.py     RazorpayExecutor filled in (DRY_RUN default)
fixtures/webhooks/*.json     5 payloads
tests/test_webhooks.py       21 tests
```

**No live tunnel.** This box has no `ngrok`/`cloudflared`, no Razorpay
credentials, and the pinned `razorpay==1.4.2` SDK is broken on Python 3.12
(`pkg_resources` was removed in setuptools 81+; needs `pip install setuptools`
or an SDK bump). So the fixtures are the demo path, which TASKS.md P7 permits.
**Say this in the README** — the sanctioned wording is that the payload shape is
what matters, not whether the tunnel held.

The fixtures are hand-built from Razorpay's documented event schema, **not
captured from a live account.** Field names and nesting are real; the ids are
`_TEST` placeholders. Do not claim they are live captures.

What is genuinely exercised: HMAC-SHA256 over the raw body, 400 on bad or
missing signature, `UNIQUE(events.dedupe_key)` absorbing retries, error_reason →
`FailureClass`, obligation keyed on order/invoice/subscription rather than
payment id, and success events closing cases + cancelling pending actions.

`RazorpayExecutor.execute` never debits a card. `DRY_RUN=true` returns a
would-do string; with `DRY_RUN=false` it creates payment links but records
RETRY as `INTENT_ONLY`. A server-initiated debit from a hackathon build is not
a reversible action.

If time runs short, **cut P7 and P8 and go straight to P9.** The submission is
already coherent without them: the chaos tab shows the failure handling, and
`kill_llm` already demonstrates that the LLM is optional by construction.

## Contracts you must not break

**Posterior duck type.** Anything passed to `decide()` as `posterior` needs
`.sample(segment, action, rng) -> float`, `.mean(...)`, `.update(segment, action, success)`.
`Posterior` and `FixedPosterior` both satisfy it.

**`rng` is always a seeded `numpy.random.Generator`, always passed in.** Never
`np.random.*` at module level. Same seed must reproduce the benchmark exactly.

**`sim/` may import from `app/`. `app/` may NEVER import from `sim/`.**
If the engine can read `Truth`, the benchmark is a lie and the project is worthless.
Grep for it before every commit.

**`build_snapshot` for the live path goes in `app/controllers/`, not `app/domain/`.**
Mirror `sim/runner.py::snapshot_of` — same fields, sourced from repos instead of the world.

## Behaviour that will look like a bug and is not

- **An expired card at rung 0 produces `REMIND`, not `METHOD_CHANGE`.** RETRY
  (rung 1) is gate-blocked so the ladder skips it and stops at the first legal
  rung. The case climbs to METHOD_CHANGE over later decisions. Gradual
  escalation is the intent.
- **A STOP does not close the obligation.** We stop *acting*; the customer may
  still pay on their own, and that money is not ours to claim. This is why
  `left_alone` cases still show up in recoveries.
- **`WAIT` scores exactly 0.0**, so `best_ev <= 0 -> WAIT` is the "leave them
  alone" branch. Roughly half of all cases should land there. If almost none do,
  the friction costs in `config/default.yaml` are too low.
- **`ActionType.NONE` is never executed.** It exists so the bandit can estimate
  the do-nothing counterfactual, and it is fed exclusively by the CONTROL arm in
  `run_arm`. If `NONE` stops receiving updates, uplift silently collapses into
  raw probability and the entire thesis dies while everything still appears to run.
- **The chaos endpoints seed their own throwaway obligations** in the sqlite db.
  They are self-contained and safe to press repeatedly on camera.
- **Replay uses `FrozenPosterior`,** built from the probabilities stored on the
  decision itself. Replay must answer "would we decide the same given what we
  knew AND what the model believed then" — using today's posterior would be a
  different question. Storing the whole table per decision would be enormous.
- **`BASELINE_RECHECKS_BEFORE_SEND = False`** in `sim/runner.py` is a modelled
  choice, stated openly: fixed-schedule tools fire without re-reading payment
  state, which is where their false-chase rate comes from. Flip it to `True` and
  that gap closes while our gate and uplift advantages remain. Mention this in
  the README — volunteering it is worth more than hiding it.
