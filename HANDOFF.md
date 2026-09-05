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
app/services/bandit.py     Beta posteriors, Thompson, EB shrinkage, per-cell decay
app/services/clock.py      RealClock | VirtualClock
app/config_loader.py       yaml -> frozen Config
app/metrics.py             bootstrap CI, Wilson, scoreboard, where-we-lost
sim/world.py               archetypes, HiddenTruth, 6 presets
sim/runner.py              four-arm harness, one world COPY per arm (3z)
run_benchmark.py           the experiment
main.py                    FastAPI entrypoint
config/default.yaml        every tunable, already chosen
tests/                     349 passing tests (290 at P6; batch 3 adds to this)
```

**TASKS.md P0 through P6 are DONE.** Start at P7.

### Verify before you touch anything

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -q                                  # 349 passed
PYTHONPATH=. python run_benchmark.py --n 2000 --preset default
PYTHONPATH=. python main.py                                    # localhost:8000
```

### The headline number

**Incremental: Rs 897,958 (mean of 5 seeds, range Rs 779k – Rs 1,028k)**

Quote this, not a single seed. A judge who reruns with a different seed lands
inside that range, which is the point of stating it. The single-seed run below is
the reproducible reference, not the claim.

Expected (seed 42, n=2000). Byte-identical on every run — verified three times:

```
  arm              recovered     rate   contacts   actions   written off
  CONTROL         Rs 892,622    26.9%          0         0         1,423
  BASELINE      Rs 1,308,719    39.5%      1,570     1,541         1,206
  ENGINE        Rs 1,846,673    55.7%      3,197     3,197           923
  ORACLE        Rs 2,164,541    65.3%      3,044     3,523           663

  at risk            Rs 3,314,887
  organic (control)  Rs 892,622   <- money that arrived anyway
  INCREMENTAL        Rs 954,052
  net of reversals   Rs 930,001
    case-level CI      [Rs 759,173 .. Rs 1,161,879]   (bootstrap within this run)
  % of oracle ceiling   75.0%

  false chase /10k   engine 0.0   baseline 145.0
  left alone         1,021 cases  (Rs 1,178,987 deliberately not chased)
  written off        923 cases  (Rs 1,468,214)
```

`md5 763fcd5cb36db1593189c08f2c59c70e` over that stdout. Item 3z moved it (each
arm now gets its own copy of the world); 3c moved it before that.

There is no `double charges` line, and there should not be. It was removed at
item 3d: nothing ever incremented it, so it could only report 0, and a safety
counter stuck at 0 by the absence of the thing it measures reads as a prevented
harm. `/api/scoreboard` now returns `"not applicable -- no live debits are
issued"` with the reason attached. See README, "three dead metrics".

### Two uncertainty numbers, not one

`--seeds 42,43,44,45,46` reruns the whole four-arm benchmark per seed (n=2000):

```
  seed         incremental   % of ceiling
  42            Rs 954,052          75.0%
  43            Rs 778,815          67.4%
  44          Rs 1,028,039          80.4%
  45            Rs 888,983          72.1%
  46            Rs 839,904          72.9%

  mean incremental      Rs 897,958
  seed-to-seed range    [Rs 778,815 .. Rs 1,028,039]   <- across 5 independent runs
  case-level CI (mean)  [Rs 671,202 .. Rs 1,139,296]   <- bootstrap within one run
  % of oracle ceiling   73.6%   [67.4% .. 80.4%]
```

The bootstrap CI resamples cases inside one run, so it only sees case-level
variance. The seed sweep redraws the world and every action roll. **Quote both.**
Headline the mean (Rs 897,958), not seed 42's Rs 954,052 — one seed is one draw.

Note the seed range (Rs 249k wide) came out *narrower* than the case-level CI
(Rs 468k wide). Five seeds is a small sample for a range and the bootstrap is
genuinely wide at n=2000, so read them as complementary, not one superseding
the other.

**This is the only uncertainty statement in the project that can carry weight.**
Item 3c established that a single seed is chaotically unstable — see README, "a
rounding error moves the numbers as far as the feature does". Perturbing the
bandit's beta parameters in the seventh decimal moves single-seed engine recovery
by ~4 points, because Thompson sampling argmaxes over near-tied candidates and one
flipped comparison diverges the run. So: quote the mean, quote the range, and do
not attribute any single-seed delta to any code change.

### Why the numbers moved at item 3a, and which direction is which

Item 3a taught G8 that a case with no mandate holds no instrument, so RETRY there
cannot reach money.

**Read this table as "which direction", not "by how much".** It reports one seed,
and item 3c established that a single seed is not a measuring instrument for a
code change — a one-in-a-million perturbation of the bandit arithmetic moves a
single-seed number by ~4 points. So the *sign* of each move below is attributable
(the gate blocks retries, so the engine must shed retry-based money and the oracle
must shed it too), and the *magnitudes* are not. Treat every percentage here as ±a
few points, at minimum.

```
                       before 3a      today
  BASELINE            Rs 1,539,397   Rs 1,308,719     down
  ENGINE              Rs 1,895,364   Rs 1,846,673     down
  ORACLE              Rs 2,438,891   Rs 2,164,541     down
  INCREMENTAL         Rs 1,002,742     Rs 954,052     down
  % of oracle ceiling        64.8%          75.0%     up
```

The engine recovers *less money* and captures *more of the ceiling*. Both are
real. `sim/world.py` gives RETRY a genuine success probability on these cases
(0.60 on ISSUER_DOWN, 0.45 on NETWORK_ERROR) and that matrix is an input — it was
not touched to make this look better. So the simulator still pays out for a retry
that the live executor reports back as `INTENT_ONLY: server-initiated debit not
enabled`. Item 3a stops the engine collecting that fake money.

BASELINE should fall hardest: a fixed retry-first policy is precisely what the
gate takes away. ORACLE should fall too — the ceiling itself was partly built on
retries that cannot happen — and the ratio should rise because the denominator got
honest faster than the numerator did. Each of those directions is what the table
shows. The sizes are not evidence; the seed sweep is the only instrument with a
resolution, and even it only resolves to the range in "Two uncertainty numbers".

Note the earlier version of this section quoted ORACLE falling 11.4% and read it
as the ceiling getting honest. That magnitude was never attributable and this
table no longer claims it. See README, "a rounding error moves the numbers as far
as the feature does".

**The number went down and the claim got stronger.** Rs 9,54,052 that could
actually be collected beats Rs 10,02,742 that partly could not.

The right-hand column is today's number, which item 3z lowered again for the same
kind of reason — see "the four arms are not independent" below.

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
48.7%, the largest baseline move in the table, while our ceiling share falls to
the lowest of the six.

`link_friendly` (PAY_LINK + METHOD_CHANGE × 2) **does not do the job it was asked
to do, and the table says so.** The intent was "does the dumb schedule nearly
catch us when its primary lever works well, on a lever 3a can't neutralise". But
the baseline never sends PAY_LINK or METHOD_CHANGE at all, so the multiplier
cannot reach it: the baseline column does not move by one paisa. Only the engine
gains, and our ceiling share goes **up**. It is therefore a labelled best-case
showcase, not evidence of fairness, and it is reported as exactly that in
README.md and in the `sim/world.py` preset comment.

If CONTROL ever shows contacts > 0 or actions > 0, stop everything: gate G0 is
broken and the headline number is fiction.

### All six worlds (`--all-presets`, n=1500) — this table is in the README

| preset | control | baseline | engine | oracle | incremental | % of ceiling | evidence for |
|---|---|---|---|---|---|---|---|
| default | 25.9% | 37.7% | 53.4% | 64.8% | Rs 690,581 | 70.6% | the headline |
| high_organic | 43.7% | 48.2% | 61.4% | 71.0% | Rs 422,030 | 64.8% | **costs us** |
| remind_friendly | 25.9% | 48.7% | 62.7% | 73.7% | Rs 923,697 | 76.9% | **costs us** |
| noisy | 25.9% | 37.8% | 54.8% | 67.3% | Rs 726,782 | 69.8% | *barely any more* |
| retry_friendly | 25.9% | 37.7% | 53.4% | 64.9% | Rs 690,581 | 70.5% | *nothing any more* |
| link_friendly | 25.9% | 37.7% | 67.3% | 77.1% | Rs 1,039,460 | 80.7% | *flatters us* |

**Single seed each. Read the columns, not the deltas** — same reason as the 3a
table above. What this table supports is the ranking and the shape: which world
has the highest control arm, which one moves the baseline, which one moves only
us. What it does not support is "preset X costs us N points".

`high_organic` cuts our incremental by nearly 40% (Rs 422,030 vs Rs 690,581) and
is the only world where control alone recovers 43.7% — that one still does its
job, and it is structural rather than a seed artifact because the preset raises
`self_pay` directly. It also now takes the largest bite out of our ceiling share
of any world in the table. `remind_friendly` moves the baseline furthest of
anything here, by 11 points, which is the point of it and is large enough to
survive the noise.

`noisy` has **almost stopped costing us.** It used to take 2 points of engine
recovery and 10 of ceiling share; it now shows engine recovery slightly higher
than `default` and ceiling share one point lower, and one point is inside the
single-seed noise. The honest reading is that it no longer discriminates at n=1500
seed 42 — not that noise has become free.

`retry_friendly` now reads identically to `default` on the first three columns.
See "What item 3a cost the preset sweep" above: that is a consequence of the
no-mandate gate, not a copy-paste error, and it means this preset has stopped
being evidence. **Report all six anyway. Do not tune them away** — including the
two that no longer discriminate and the one that flatters us, because hiding any
of them would be the actual dishonesty.

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
`REMIND uplift -0.0293, EV -Rs 200.64 -> WAIT`, and matches.

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
tests/test_downtime.py       26 tests (item 3b)
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
  timer; past `STALE_DOWNTIME_HOURS` (default 6, live-path env knob) the Ops
  attention row turns **STALLED** and says what it has cost and what the two
  explanations are. That is a label, not an expiry — the row stays `started` and G9
  keeps holding, because auto-clearing would be the end-time guess 3b refuses.
  `POST /api/demo/downtime?method=upi` opens one on camera with a timestamp of now,
  `&minutes_ago=400` makes it STALLED, `&resolve=true` clears it.

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
- **`n` in the posterior table can go DOWN.** Item 3c added `bandit.decay` (0.999),
  and `count()`/`table()` report decayed evidence rather than arrivals. A cell that
  has seen 5000 observations reports ~1000, because that is how much of it is still
  load-bearing. Reporting arrivals would hide exactly the failure that got global
  decay rejected — see the `app/services/bandit.py` module docstring, which has the
  measurement. **Forgetting is per-cell: a cell ages only when written.** Never
  "fix" it into global decay; `tests/test_bandit.py::test_the_counterfactual_survives_a_full_engine_arm`
  is there to stop you, because global decay eats the CONTROL arm's NONE burst and
  leaves uplift measured against 53 observations out of 2000. `bandit.decay: 1.0`
  turns it off and exactly recovers the pre-3c arithmetic.

- **Replay uses `FrozenPosterior`,** built from the probabilities stored on the
  decision itself. Replay must answer "would we decide the same given what we
  knew AND what the model believed then" — using today's posterior would be a
  different question. Storing the whole table per decision would be enormous.
- **`BASELINE_RECHECKS_BEFORE_SEND = False`** in `sim/runner.py` is a modelled
  choice, stated openly: fixed-schedule tools fire without re-reading payment
  state, which is where their false-chase rate comes from. Flip it to `True` and
  that gap closes while our gate and uplift advantages remain. Mention this in
  the README — volunteering it is worth more than hiding it.

## FIXED in item 3z: the four arms were not independent

Found while measuring item 3c, fixed as its own item. Kept here because the
numbers everywhere in this file are the *post*-fix ones, and a reader comparing
against an older README or an older commit needs to know why they moved.

`sim/runner.py` does `tr.self_pay_at = None` in `_execute` — the sleeping-dog
effect, where contacting a customer who would have paid on their own kills that
self-payment. It mutates `World`. `run_once` built one world and passed it to all
four arms in sequence, so BASELINE's kills were still gone when ENGINE ran, and
BASELINE's *and* ENGINE's were gone when ORACLE ran. The arms were not four
independent draws on the same world; they were a chain.

Measured at n=2000 seed 42, before and after:

```
  arm          shared world (was)      own world (now)      delta
  CONTROL              Rs   892,622    Rs   892,622             0   (never acts)
  BASELINE             Rs 1,308,719    Rs 1,308,719             0   (runs first)
  ENGINE               Rs 1,870,324    Rs 1,846,673    -Rs 23,651
  ORACLE               Rs 2,163,470    Rs 2,164,541     +Rs 1,070
  INCREMENTAL          Rs   977,702    Rs   954,052    -Rs 23,650
  % of oracle ceiling         76.9%           75.0%       -1.9pt
```

CONTROL and BASELINE do not move: CONTROL never acts, and BASELINE runs first, so
nothing had mutated the world before them. Only the arms downstream in the chain
move, which is the signature of the bug rather than of noise.

**The direction was the finding; the size was not.** BASELINE kills exactly 2
self-payers, worth about Rs 3.3k at the mean case size, so most of the Rs 23.6k is
the chaotic divergence documented in the README rather than the direct value of
two customers. What was attributable: the contamination was real, it flowed
downstream only, and it flowed in the flattering direction — ENGINE was scored on
a world where BASELINE had already burned self-payers ENGINE would otherwise have
had to resist contacting.

The fix is `w = copy.deepcopy(w)` at the top of `run_arm`, deliberately placed
there rather than in `run_once` so a caller cannot forget it.
`tests/test_arm_independence.py` pins the property from the outside: an arm run
alone must produce byte-identical results to the same arm run fourth. One of those
four tests exists specifically to stop the others passing for the wrong reason —
if isolation ever became *deletion* of the sleeping-dog effect, the independence
assertions would go green while the simulator quietly stopped modelling the thing
the whole sleeping-dog story is about.
