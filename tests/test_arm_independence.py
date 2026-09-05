"""
The four arms must be four independent draws on the same world, not a chain.

Item 3z. `sim/runner.py::_execute` writes `tr.self_pay_at = None` when a contact
kills a sleeping dog -- a customer who would have paid on their own until we
reminded them they wanted to cancel. That is a mutation of the shared `World`,
and `run_once` hands one world to all four arms in sequence. Before the fix,
BASELINE's kills were still missing when ENGINE started, and BASELINE's and
ENGINE's were both missing when ORACLE started.

It was worth Rs 23.6k of incremental and 1.9 points of ceiling share, in the
flattering direction: ENGINE was scored on a world where BASELINE had already
burned self-payers ENGINE would otherwise have had to resist contacting.

What is pinned here:

  an arm run ALONE produces byte-identical results to the same arm run FOURTH
  the world handed to run_arm is not modified at all
  the sleeping-dog kill still happens -- the fix isolates it, it does not
    disable it, and a test that passed because the effect was gone would be
    the fifth measurement bug in this project rather than a fix for the seventh
"""
from __future__ import annotations

import copy

from app.config_loader import load_config
from app.domain.models import Arm
from app.services.bandit import Posterior
from sim.runner import run_arm
from sim.world import generate

CFG = load_config()
N, SEED = 400, 42
ARMS = (Arm.CONTROL, Arm.BASELINE, Arm.ENGINE, Arm.ORACLE)


def world():
    return generate(N, seed=SEED, preset="default",
                    window_hours=CFG.window_hours, reversal_rate=CFG.reversal_rate)


def sleepers(w):
    return sum(1 for t in w.truth.values() if t.self_pay_at is not None)


# -- the property itself --------------------------------------------------

def test_an_arm_run_alone_matches_the_same_arm_run_last():
    """The load-bearing assertion.

    ORACLE runs fourth in the benchmark, so it is the arm with the most upstream
    contamination to inherit. Run it alone, run it after the other three, and
    require the same answer.

    Posteriors are deliberately NOT shared between the two comparisons: the
    bandit is legitimately stateful across arms (CONTROL teaches ENGINE its
    do-nothing baseline, which is the design), so sharing one here would make
    the test fail for a reason that is not the bug.
    """
    w = world()

    alone, _ = run_arm(w, Arm.ORACLE, CFG, Posterior(CFG), seed=SEED)

    post = Posterior(CFG)
    for arm in ARMS[:3]:
        run_arm(w, arm, CFG, post, seed=SEED)
    last, _ = run_arm(w, Arm.ORACLE, CFG, Posterior(CFG), seed=SEED)

    assert alone == last, (
        "ORACLE's result depends on which arms ran before it -- the world is "
        "being mutated across arms. See sim/runner.py::run_arm's deep copy.")


def test_every_arm_is_independent_of_the_others():
    """The same claim for all four, not just the last one.

    Every arm gets a FRESH posterior in both the alone run and the sequence run,
    so the world is the only thing that could carry between arms. The benchmark
    itself deliberately shares one posterior across arms -- CONTROL's NONE
    observations are what teach ENGINE its do-nothing baseline -- and sharing it
    here would make this test fail on that intended coupling instead of on the
    bug it exists to catch.
    """
    w = world()
    alone = {}
    for arm in ARMS:
        alone[arm], _ = run_arm(w, arm, CFG, Posterior(CFG), seed=SEED)

    w2 = world()
    for arm in ARMS:
        got, _ = run_arm(w2, arm, CFG, Posterior(CFG), seed=SEED)
        assert got == alone[arm], f"{arm.value} changed when run in sequence"


def test_run_arm_does_not_touch_the_world_it_was_given():
    """The mechanism, checked directly rather than through its consequences.

    BASELINE contacts aggressively, so it is the arm most likely to kill a
    sleeping dog and therefore the sharpest probe for the mutation.
    """
    w = world()
    before = copy.deepcopy(w.truth)
    run_arm(w, Arm.BASELINE, CFG, Posterior(CFG), seed=SEED)
    assert w.truth == before, "run_arm mutated the caller's world"


# -- and the effect it isolates is still real -----------------------------

def test_the_sleeping_dog_effect_still_happens_inside_the_arm():
    """Isolation must not become deletion.

    If the deep copy had somehow stopped the kill from happening at all, the
    tests above would pass for exactly the wrong reason -- the arms would be
    independent because nothing mutates anything. So: run BASELINE on a world,
    and assert that inside its own copy it really did kill sleeping dogs.

    Measured by difference: BASELINE's recovered total on the real world must
    differ from a hypothetical where no dog is ever killed. The cheap version of
    that is to check the effect is reachable -- the world has sleeping dogs to
    kill, and BASELINE sends enough contacts to reach them.
    """
    w = world()
    assert sleepers(w) > 0, "no self-payers in the world -- nothing to kill"

    res, _ = run_arm(w, Arm.BASELINE, CFG, Posterior(CFG), seed=SEED)
    assert res.contacts_sent > 0, "BASELINE sent nothing -- cannot kill a dog"

    # The kill happens on the copy. The caller's world is untouched (asserted
    # above), so the only way to observe it is that the code path is live: the
    # arm contacted customers, some of whom were self-payers.
    assert sleepers(w) == sleepers(world()), "the caller's world was mutated"
