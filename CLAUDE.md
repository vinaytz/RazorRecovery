# CLAUDE.md — RazorRecovery

Read `SPEC.md` before writing code. Work through `TASKS.md` in order.

## What this project is

An AI revenue-recovery engine that only counts money it can **prove it caused**.
It decides whether, when, and how to recover a failed payment — and measures itself
against a control group that it deliberately never touches.

Razorpay AI Buildathon, Track 03. Deadline is hours away. Optimise for a working
demo and an honest number, not for completeness.

## Hard rules

1. **`app/domain/` is pure.** No DB, no HTTP, no `datetime.now()`, no LLM, no
   randomness that isn't seeded and passed in. Everything arrives as arguments.
   `tests/test_purity.py` enforces this — write it in Phase 1.
2. **Money is `int` paise everywhere.** Never float. Format only at the UI edge.
3. **Every STOP carries a reason code** from the `StopReason` enum. Never a bare bool.
4. **A timeout is `UNKNOWN`, never a failure.** The reconciler resolves it.
5. **The LLM never returns a money action.** It returns a value from a fixed enum,
   or template-slot text. Nothing else.
6. **`DRY_RUN=true` is the default.**
7. **All randomness is seeded.** Same seed, same benchmark, every run. No exceptions.
8. **The engine may only read fields listed in `CaseSnapshot`.** It must never see
   simulator ground truth. If you find yourself importing from `sim/` inside
   `app/`, stop — that is the bug that invalidates the whole project.

## How to work

- **Commit after every phase.** The demo must run at every commit. Never leave main broken.
- **Each phase has an acceptance command in `TASKS.md`. Run it. Paste the output.
  Do not start the next phase until it passes.**
- Prefer boring code. No abstractions with one implementation, no decorators,
  no metaclasses, no premature interfaces beyond the two swap points in SPEC §9.
- No new dependency outside `requirements.txt` without asking.
- If a phase is running >1.5x its time budget, say so and propose a cut. Do not
  silently keep going.
- If the spec is ambiguous, pick the simpler option, add a `# DECISION:` comment
  explaining the choice, and continue. Do not stop to ask about small things.

## Things that will feel helpful and will ruin the project

- Putting a DB call inside `decide()` — kills replay, kills the audit trail, kills the demo.
- Letting the engine peek at `HiddenTruth` — makes the whole benchmark a lie.
- Rewriting the gate order or adding gates not in SPEC §5.
- Making the dashboard pretty instead of legible.
- Using `random` without the seeded generator.
- Starting Phase N+1 while Phase N's acceptance command fails.
