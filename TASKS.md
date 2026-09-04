# RazorRecovery — TASKS

> **STATUS: P0–P6 are already built, tested and passing.** See `HANDOFF.md`.
> Verify with `PYTHONPATH=. pytest tests/ -q` and
> `PYTHONPATH=. python run_benchmark.py --n 2000`, then **start at P7**.
> The phases below are kept for context and for the acceptance criteria.

Work these in order. **Run the acceptance command. Paste the output. Commit. Then move on.**
Never start a phase while the previous one's acceptance fails.

Time budget assumes ~9 hours total. The last 90 minutes are frozen for README + video.

---

## P0 · 20 min — Scaffold  ✅ DONE

- `requirements.txt`: fastapi uvicorn sqlalchemy pydantic numpy scipy pyyaml python-dotenv google-generativeai razorpay pytest
- Folder tree per SPEC §2, all `__init__.py`
- `config/default.yaml` (provided — do not invent values)
- `repos/db.py`: SQLAlchemy Core engine, `init_db()` creating SPEC §13 tables
- `main.py`: FastAPI app, `/health`, serves `web/index.html` at `/`
- `tests/test_purity.py`: walks `app/domain/*.py` ASTs, fails if any import references
  `repos`, `services`, `sim`, `datetime.now`, `requests`, or bare `random`

**Accept:** `pytest tests/test_purity.py && python main.py &` → `curl localhost:8000/health` → `{"ok":true}`

---

## P1 · 70 min — Simulator + snapshot  ✅ DONE

- `sim/world.py`: `WorldConfig` with the four presets from SPEC §10.
  `generate(n, seed, preset) -> (list[Obligation], list[Customer], HiddenTruth)`
- Archetype mix, `p_self`, `responsiveness`, `self_pay_at`, `true_p[action]` per SPEC §10
- `HiddenTruth` lives ONLY in `sim/`. Nothing in `app/` may import it.
- `domain/models.py`: `CaseSnapshot` exactly as SPEC §4, plus `Decision`, `GateResult`
- `build_snapshot(case, obligation, customer, contacts, downtimes, now) -> CaseSnapshot`

**Accept:** `python -c "from sim.world import generate; o,c,h=generate(5000,42,'default'); print(len(o), sum(1 for x in h.values() if x.self_pay_at), h[o[0].id])"`
→ runs in <5s, ~20–30% have a `self_pay_at`. Run twice with seed 42 → identical output.

---

## P2 · 80 min — Gates, ladder, engine, four arms ⭐  ✅ DONE

**This is the finish line. Everything after is upgrades.**

- `domain/gates.py` — SPEC §5, exact order, full trace
- `domain/ladder.py` — SPEC §6
- `domain/scoring.py` — SPEC §7, but with a **fixed lookup table** for now, not the bandit
- `domain/allocator.py` — SPEC §8
- `domain/engine.py` — `decide(snap, config, posterior, rng) -> Decision`
- `sim/runner.py` — event loop over the virtual timeline, four arms, same seed
- `run_benchmark.py` — writes `results.json` per SPEC §12

**Accept:** `python run_benchmark.py --n 5000 --seed 42 --preset default`
prints a table with four arms and a non-zero `incremental` with a CI.
`CONTROL` must show `contacts_sent == 0` and `actions_taken == 0`. If it doesn't, G0 is broken.

**Commit and tag this `v0-submittable`.** You now have a project.

---

## P3 · 50 min — Bandit  ✅ DONE

- `services/bandit.py`: Beta posteriors incl. `action=NONE`, Thompson sample,
  empirical-Bayes shrinkage, update on outcome
- Swap `scoring.py` off the lookup table onto the posterior
- Bandit learns online during the run — start from uninformative priors

**Accept:** benchmark rerun → engine's incremental ≥ P2's. Print the posterior table
for `CARD_EXPIRED|*`: `METHOD_CHANGE` must have learned a higher mean than `RETRY`.
Print `sleeping_dogs_avoided > 0`.

---

## P4 · 30 min — All four worlds  ✅ DONE

Run all presets, write `results_all.json`.

**Accept:** `python run_benchmark.py --all-presets` → four result blocks.
`retry_friendly` should show a much smaller edge over baseline. **This is expected and good — do not tune it away.**

---

## P5 · 80 min — Dashboard  ✅ DONE

`web/index.html`, Chart.js CDN, three views per SPEC §16.
`api/dashboard.py` + `api/replay.py`.

**Accept:** open `localhost:8000` → four bars render with the gap labelled →
click a case → decision card shows gate trace and candidate scores →
click Replay → `match: true`.

---

## P6 · 40 min — Chaos + failure handling  ✅ DONE

- `api/chaos.py`: `duplicate_webhook`, `kill_llm`, `executor_timeout`, `pay_midflight`
- `controllers/execute.py` re-check + `UNKNOWN` handling per SPEC §14
- `workers/reconciler.py`: resolves `UNKNOWN` actions
- `tests/test_idempotency.py`: same event twice → one event row, one action

**Accept:** press each of the four buttons. Duplicate → one action. Kill LLM →
decisions keep flowing. Timeout → status `UNKNOWN` → reconciler resolves it.
Pay mid-flight → action aborts `ALREADY_SETTLED`, no contact sent, false-chase counter unchanged.

---

## P7 · 45 min — Razorpay test mode ⏰ HARD TIMEBOX

Tunnel, webhook secret, signature verification on raw body, ingest controller,
success events closing cases. Save 3–5 real payloads to `fixtures/webhooks/`.

**Accept:** one real test-mode payment fails → webhook arrives → case appears in dashboard.

**At 45 minutes, stop regardless of state.** Fallback: replay `fixtures/webhooks/*.json`
through the same endpoint and say so in the README. The payload shape is what matters,
not whether the tunnel held.

---

## P8 · 30 min — LLM layers

`StubLLM` first, then `GeminiLLM`. Jobs ①, ③, ④ per SPEC §17. On-disk cache keyed
on `sha1(text)`. PII tokenised.

**Accept:** `LLM_MODE=stub` → benchmark runs identically. `LLM_MODE=gemini` → error
classification works and the cache shows ~40 calls for 5,000 cases. Decision card
shows a plain-English narration.

---

## P9 · 90 min — README + video 🔒 NO CODE

**Set an alarm. This block is the submission.**

README first screen, in this order:
1. One line: *"An AI recovery engine that only counts money it can prove it caused."*
2. The four-bar chart as a PNG
3. The pipeline diagram (ASCII is fine)
4. "AI is used in 4 places. It decides in 0."
5. "Synthetic data — treat absolute values as directional. The method is the contribution."
6. `pip install -r requirements.txt && python main.py`

Then: architecture, the four worlds table, the honesty metrics, what was cut and why,
the judge-questions table.

Video, 5:00 exactly:

| Time | Beat |
|---|---|
| 0:00 | Hook — most recovery tools bill for money that was coming anyway. Four bars on screen. |
| 0:20 | The problem, fast. They know what a failed payment is. |
| 1:00 | Architecture: gates → ladder → uplift → allocator. Where the AI is and isn't. |
| 2:00 | Sleeping-dog demo: engine says STOP on a large case. Open the decision card. |
| 3:00 | Chaos: kill the LLM live, duplicate webhook, timeout → UNKNOWN → reconciled. |
| 4:00 | Razorpay test mode: real webhook, same code path. |
| 4:40 | % of oracle captured, false-chase rate, "where we lost". End. |

Lead with the number. Never explain the problem for more than a minute.

---

## Cut order if behind

Cut from the bottom. P2 alone is a submission; P2+P5+P9 is a good one.

```
P9  README + video       ← never cut. this IS the submission.
P2  four-arm benchmark   ← never cut. without this there is nothing.
P5  dashboard + replay
P6  chaos
P3  bandit               (the lookup table still tells the story)
P4  four worlds          (default preset alone is fine)
P7  live Razorpay        (fixtures are an honest fallback)
P8  LLM                  (StubLLM still demonstrates the architecture)
```
