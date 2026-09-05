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
sim/world.py               archetypes, HiddenTruth, 6 presets
sim/runner.py              four-arm harness on a virtual clock
run_benchmark.py           the experiment
main.py                    FastAPI entrypoint
config/default.yaml        every tunable, already chosen
tests/                     290 passing tests
```

**TASKS.md P0 through P6 are DONE.** Start at P7.

### Verify before you touch anything

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -q                                  # 290 passed
PYTHONPATH=. python run_benchmark.py --n 2000 --preset default
PYTHONPATH=. python main.py                                    # localhost:8000
```

### The headline number

**Incremental: Rs 850,108 (mean of 5 seeds, range Rs 758k – Rs 966k)**

Quote this, not a single seed. A judge who reruns with a different seed lands
inside that range, which is the point of stating it. The single-seed run below is
the reproducible reference, not the claim.

Expected (seed 42, n=2000). Byte-identical on every run — verified three times:

```
  arm              recovered     rate   contacts   actions   written off
  CONTROL         Rs 892,622    26.9%          0         0         1,423
  BASELINE      Rs 1,308,719    39.5%      1,570     1,541         1,206
  ENGINE        Rs 1,858,625    56.1%      3,205     3,205           924
  ORACLE        Rs 2,159,975    65.2%      3,044     3,523           667

  at risk            Rs 3,314,887
  organic (control)  Rs 892,622   <- money that arrived anyway
  INCREMENTAL        Rs 966,003
  net of reversals   Rs 943,227
    case-level CI      [Rs 778,422 .. Rs 1,174,428]   (bootstrap within this run)
  % of oracle ceiling   76.2%

  false chase /10k   engine 0.0   baseline 145.0
  left alone         1,036 cases  (Rs 1,192,089 deliberately not chased)
  written off        924 cases  (Rs 1,456,262)
```

### Two uncertainty numbers, not one

`--seeds 42,43,44,45,46` reruns the whole four-arm benchmark per seed (n=2000):

```
  seed         incremental   % of ceiling
  42            Rs 966,003          76.2%
  43            Rs 797,659          69.7%
  44            Rs 758,480          59.7%
  45            Rs 924,451          76.2%
  46            Rs 803,949          70.0%

  mean incremental      Rs 850,108
  seed-to-seed range    [Rs 758,480 .. Rs 966,003]   <- across 5 independent runs
  case-level CI (mean)  [Rs 622,442 .. Rs 1,084,224]   <- bootstrap within one run
  % of oracle ceiling   70.3%   [59.7% .. 76.2%]
```

The bootstrap CI resamples cases inside one run, so it only sees case-level
variance. The seed sweep redraws the world and every action roll. **Quote both.**
Headline the mean (Rs 850,108), not seed 42's Rs 966,003 — one seed is one draw.

Note the seed range (Rs 208k wide) came out *narrower* than the case-level CI
(Rs 462k wide). Five seeds is a small sample for a range and the bootstrap is
genuinely wide at n=2000, so read them as complementary, not one superseding
the other.

### Why the numbers moved at item 3a, and which direction is which

Item 3a taught G8 that a case with no mandate holds no instrument, so RETRY there
cannot reach money. Everything in this section moved as a result, and it did not
all move the same way:

```
                       before 3a      after 3a
  BASELINE            Rs 1,539,397   Rs 1,308,719     -15.0%
  ENGINE              Rs 1,895,364   Rs 1,858,625      -1.9%
  ORACLE              Rs 2,438,891   Rs 2,159,975     -11.4%
  INCREMENTAL         Rs 1,002,742     Rs 966,003      -3.7%
  % of oracle ceiling        64.8%          76.2%    +11.4pt
```

The engine recovers *less money* and captures *more of the ceiling*. Both are
real. `sim/world.py` gives RETRY a genuine success probability on these cases
(0.60 on ISSUER_DOWN, 0.45 on NETWORK_ERROR) and that matrix is an input — it was
not touched to make this look better. So the simulator still pays out for a retry
that the live executor reports back as `INTENT_ONLY: server-initiated debit not
enabled`. Item 3a stops the engine collecting that fake money.

BASELINE falls hardest because a fixed retry-first policy is precisely what the
gate takes away. ORACLE falls too — the ceiling itself was partly built on retries
that cannot happen — and the ratio rises because the denominator got honest faster
than the numerator did.

**The number went down and the claim got stronger.** Rs 9,66,003 that could
actually be collected beats Rs 10,02,742 that partly could not.

### What item 3a cost the preset sweep, and what was added to replace it

`retry_friendly` doubles retry success, and it exists to catch a rigged
simulator: if we still beat the dumb tool in a world built to favour dumb
retries, something is wrong. After 3a its control, baseline and engine columns
are **identical to `default`** — the gate blocks retries on every non-mandate
case, so the preset's only lever now moves ~25% of the corpus (subscriptions,
the ones that hold a mandate) and the two worlds converge.

That is a real loss of test power, stated rather than hidden. Two presets were
**added** to restore it — existing presets were not edited, and every world that
predates them draws a byte-identical random stream (the effect loop reads the new
multipliers behind `in p` guards; `tests/test_presets.py` pins that).

`remind_friendly` (REMIND × 2) is the replacement, and it works. After 3a the
fixed schedule's only surviving lever is REMIND — `baseline_decide` sends RETRY
and REMIND and nothing else — so REMIND is the one thing that can be made to work
unusually well and have the BASELINE actually feel it. It lifts baseline 37.7% →
48.7% while our ceiling share falls 80.7% → 73.4%.

`link_friendly` (PAY_LINK + METHOD_CHANGE × 2) **does not do the job it was asked
to do, and the table says so.** The intent was "does the dumb schedule nearly
catch us when its primary lever works well, on a lever 3a can't neutralise". But
the baseline never sends PAY_LINK or METHOD_CHANGE at all, so the multiplier
cannot reach it: the baseline column does not move by one paisa. Only the engine
gains, and our ceiling share goes **up**, 80.7% → 87.9%. It is therefore a
labelled best-case showcase, not evidence of fairness, and it is reported as
exactly that in README.md and in the `sim/world.py` preset comment.

If CONTROL ever shows contacts > 0 or actions > 0, stop everything: gate G0 is
broken and the headline number is fiction.

### All six worlds (`--all-presets`, n=1500) — this table is in the README

| preset | control | baseline | engine | oracle | incremental | % of ceiling | evidence for |
|---|---|---|---|---|---|---|---|
| default | 25.9% | 37.7% | 57.0% | 64.5% | Rs 781,894 | 80.7% | the headline |
| high_organic | 43.7% | 48.2% | 64.2% | 70.0% | Rs 490,133 | 78.2% | **costs us** |
| remind_friendly | 25.9% | 48.7% | 60.9% | 73.6% | Rs 880,379 | 73.4% | **costs us** |
| noisy | 25.9% | 37.8% | 55.1% | 67.2% | Rs 734,409 | 70.7% | **costs us** |
| retry_friendly | 25.9% | 37.7% | 57.0% | 64.5% | Rs 781,894 | 80.6% | *nothing any more* |
| link_friendly | 25.9% | 37.7% | 70.9% | 77.1% | Rs 1,130,883 | 87.9% | *flatters us* |

`high_organic` cuts our edge by a third (Rs 490,133 vs Rs 781,894) and is the only
world where control alone recovers 43.7% — that one still does its job. `noisy`
costs us 2 points of engine recovery and 10 points of ceiling share, which is the
honest answer to "can it still learn when the signal is dirty". `remind_friendly`
moves the baseline furthest of anything in the table, which is the point of it.

`retry_friendly` now reads identically to `default` on the first three columns.
See "What item 3a cost the preset sweep" above: that is a consequence of the
no-mandate gate, not a copy-paste error, and it means this preset has stopped
being evidence. **Report all six anyway. Do not tune them away** — including the
one that no longer discriminates and the one that flatters us, because hiding
either would be the actual dishonesty.

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
app/controllers/ingest.py    classify, obligation identity, open/close case, downtime
app/services/executor.py     RazorpayExecutor filled in (DRY_RUN default)
fixtures/webhooks/*.json     9 payloads
tests/test_webhooks.py       21 tests
tests/test_downtime.py       18 tests (item 3b)
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

**Item 3b added the downtime feed.** `payment.downtime.started`/`.resolved` write
a row in `downtimes`, and `app/workers/live.py::snapshot` carries it into
`method_in_downtime` — before 3b that branch replied "noted" and wrote nothing, so
G9 was a gate wired to a constant `False` on the live path. Two things to know:

- **No end time is ever guessed.** A live outage sends `end: null`, so
  `downtime_ends_at` stays None and G9 falls back to `downtime_backoff_minutes`,
  which is a re-check interval and not a forecast. Only a `scheduled: true` window
  that actually carries an `end` populates it.
- **An outage defers the whole case, not just its retries.** G9 sets `wait_until`,
  and `engine.decide` returns WAIT for any gate that set one. Nothing goes out on
  that method — not a reminder, not a pay link — until a `.resolved` arrives or
  `window_hours` closes and the case is written off. Nothing expires a row on a
  timer; `GET /api/ops/attention` makes a stuck one loud instead, and a human is
  the escape hatch. `POST /api/demo/downtime?method=upi` opens one on camera with a
  timestamp of now, `&resolve=true` clears it.

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
