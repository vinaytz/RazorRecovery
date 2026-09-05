"""
Metrics and gates that are structurally incapable of reporting anything.

Item 3d. `double_charges: 0` was on the scoreboard for most of this project's
life. It reads as "we prevented double charges". Nothing in the codebase ever
incremented it -- there is no server-initiated debit to double, because
`RazorpayExecutor` records RETRY as INTENT_ONLY and the simulator has no debit
path either. It was not a prevented zero. It was an inapplicable metric wearing
a safety metric's clothes, which is the same shape as the G9 bug from item 3b
and the two dead tests before it.

Relabelling it is only half the job. The other half is finding the others, and
there are two, both about gates rather than counters:

  G2_PROMISED  never fires ANYWHERE -- benchmark or live. `promised_until` has no
    assignment in the entire codebase. The gate is correct and tested in
    isolation; nothing feeds it. Item 3e would have (a customer replying "I'll
    pay Friday"), and 3e was cut for time.
  G4_OPTED_OUT fires 114 times in the recorded benchmark and can never fire on
    the LIVE path, because `app/workers/live.py` hardcodes `opted_out=False`.
    Same field, two paths, one of them dead.

These are pinned rather than fixed. A pinned known-dead metric is honest; an
unpinned one becomes a claim the moment somebody writes a README sentence about
it. If a later item feeds one of these, its test here fails and says so, which is
the correct way to find out that a gap has closed.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from app.config_loader import load_config
from app.domain.models import ActionType, Arm
from app.services.bandit import Posterior
from sim.runner import ArmResult, run_arm
from sim.world import generate

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config()


# -- the counter that was relabelled --------------------------------------

def test_no_arm_result_field_is_a_permanently_zero_counter():
    """`double_charges` is gone from ArmResult. Do not put it back.

    If a future item genuinely implements double-charge detection, it needs an
    increment site first -- and then this test should be updated to assert the
    increment exists, not deleted.
    """
    assert not hasattr(ArmResult(arm="ENGINE", cases=0, at_risk=0), "double_charges"), (
        "double_charges is back on ArmResult. It may only exist if something "
        "increments it -- see this module's docstring.")


def test_the_scoreboard_states_double_charges_is_inapplicable():
    """Not 0. The string, and the reason."""
    from app.metrics import scoreboard

    w = generate(60, seed=1, preset="default",
                 window_hours=CFG.window_hours, reversal_rate=CFG.reversal_rate)
    post = Posterior(CFG)
    results, per_case = {}, {}
    for arm in (Arm.CONTROL, Arm.BASELINE, Arm.ENGINE, Arm.ORACLE):
        results[arm.value], per_case[arm.value] = run_arm(w, arm, CFG, post, seed=1)
    hon = scoreboard(results, per_case, n_cases=60)["honesty"]

    assert hon["double_charges"] is None, "reporting a number implies it was measured"
    assert "not applicable" in hon["double_charges_status"]
    assert "INTENT_ONLY" in hon["double_charges_why"]


def test_the_false_chase_zero_is_a_real_zero():
    """The contrast that makes the relabel meaningful.

    `false_chase_per_10k_engine` is also 0, and that one IS a result: the counter
    has live increment sites and BASELINE scores on it in the same run. A zero is
    only evidence when something else in the same instrument is non-zero.
    """
    from app.metrics import scoreboard

    w = generate(400, seed=42, preset="default",
                 window_hours=CFG.window_hours, reversal_rate=CFG.reversal_rate)
    post = Posterior(CFG)
    results, per_case = {}, {}
    for arm in (Arm.CONTROL, Arm.BASELINE, Arm.ENGINE, Arm.ORACLE):
        results[arm.value], per_case[arm.value] = run_arm(w, arm, CFG, post, seed=42)
    hon = scoreboard(results, per_case, n_cases=400)["honesty"]

    assert hon["false_chase_per_10k_baseline"] > 0, (
        "the false-chase counter reported 0 for BOTH arms -- it may have become a "
        "dead metric too, and the engine's 0 would then mean nothing")


# -- the gates nothing feeds ----------------------------------------------

def test_promised_until_has_no_assignment_anywhere():
    """G2 is a gate wired to a constant None, in both paths.

    Grepped rather than asserted through behaviour, because the claim is about
    the absence of a writer and no amount of running the system can demonstrate
    an absence. If item 3e (promise-to-pay) is ever built, this test fails and
    the docs' "designed, not fed" wording needs to become "working".

    Only WRITES count. `gates.py` reads `snap.promised_until` into `wait_until`,
    which is the gate doing its job, not a source feeding it.
    """
    writer = re.compile(r"(?:^|[^.\w])promised_until\s*=(?!=)")
    hits = []
    for d in ("app", "sim"):
        for p in sorted((ROOT / d).rglob("*.py")):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                s = line.strip()
                if not writer.search(s) or s.startswith("#"):
                    continue
                # a declaration, the None default, and deserialisation are not sources
                if (s.startswith("promised_until: ")
                        or "promised_until=None" in s
                        or "promised_until=st.promised_until" in s
                        or "promised_until=dt(" in s):
                    continue
                hits.append(f"{p.relative_to(ROOT)}:{i}: {s}")
    assert not hits, (
        "something now sets promised_until, so G2 is live. Update "
        "tests/test_dead_metrics.py and the README/HANDOFF wording that calls "
        "G2 unfed:\n  " + "\n  ".join(hits))


def test_the_live_path_can_never_trip_the_opt_out_gate():
    """G4 fires in the benchmark and cannot fire live. Same field, two paths."""
    src = (ROOT / "app" / "workers" / "live.py").read_text()
    assert "opted_out=False" in src, (
        "app/workers/live.py no longer hardcodes opted_out -- G4 may now be live "
        "on the live path, which would make the docs' claim stale")


def test_g2_never_fires_in_a_full_benchmark_run():
    """The consequence, measured end to end rather than grepped.

    Runs a small benchmark, collects every gate that stopped a case, and requires
    G2 to be absent while requiring the collection itself to be non-empty -- so
    this cannot pass by collecting nothing, which is precisely the failure mode
    this whole module is about.
    """
    env = dict(__import__("os").environ, PYTHONPATH=".", WORKER="off")
    r = subprocess.run([sys.executable, "-c", GATE_PROBE],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    fired = json.loads(r.stdout)

    assert fired, "no gate stopped anything -- the probe measured nothing"
    assert "G8_NON_RETRYABLE" in fired, "expected the busiest gate to appear"
    assert not any(g.startswith("G2") for g in fired), (
        f"G2 fired: {fired}. Something feeds promised_until now -- see above.")


GATE_PROBE = """
import json
from collections import Counter
from app.config_loader import load_config
from app.domain.models import Arm
from app.services.bandit import Posterior
from sim.recorder import Recorder
from sim.runner import run_arm
from sim.world import generate

cfg = load_config()
w = generate(300, seed=42, preset="default",
             window_hours=cfg.window_hours, reversal_rate=cfg.reversal_rate)
post = Posterior(cfg)
fired = Counter()
ids = {ob.id for ob in w.obligations}
for arm in (Arm.CONTROL, Arm.BASELINE, Arm.ENGINE, Arm.ORACLE):
    rec = Recorder("probe", ids)
    run_arm(w, arm, cfg, post, seed=42, recorder=rec)
    for row in rec.decisions:
        for g in json.loads(row[7]):        # index 7 is the gate_trace json blob
            if not g["passed"]:
                fired[g["gate"]] += 1
print(json.dumps(sorted(fired)))
"""
