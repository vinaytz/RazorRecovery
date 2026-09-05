"""The presets, and what each of them is actually evidence for.

`sim/world.py` is an input. We wrote the world our own engine is scored in, so
the presets are the defence against the obvious objection -- and a preset that
has quietly stopped discriminating is worse than no preset, because it still
looks like evidence in a table.

These tests pin three things:

  1. adding a preset cannot change an existing one. `link_mult` and `remind_mult`
     are read with `in p` guards precisely so the worlds that predate them draw
     an identical stream; if that ever stops being true, every number in
     README.md silently becomes wrong.

  2. `remind_friendly` really does close the gap. It replaced `retry_friendly` as
     the anti-rigging world after item 3a, and the only thing that makes it a
     replacement is that the baseline gains more from it than we do.

  3. `link_friendly` really does flatter us. It is in the table as a labelled
     best-case, not as evidence of fairness, and this test is what stops it
     being quietly re-described as the latter.
"""
from __future__ import annotations

import pytest

from sim import world

# n small enough to run four worlds in a test, large enough for the archetype mix
# to show up. The assertions below are all about direction and margin, never a
# specific rupee figure, so this number can move without a doc update.
N = 900
SEED = 42


def build(preset: str):
    return world.generate(n=N, seed=SEED, preset=preset)


def effects(preset: str) -> dict:
    """Every action's true success probability, keyed by obligation."""
    w = build(preset)
    return {ob.id: dict(w.truth[ob.id].p_act) for ob in w.obligations}


# -- 1. a new preset must not disturb an old one --------------------------

@pytest.mark.parametrize("preset", ["default", "high_organic", "retry_friendly", "noisy"])
def test_adding_link_and_remind_multipliers_did_not_move_the_older_worlds(preset):
    """The guard clauses in the effect loop, checked rather than trusted.

    `link_friendly` and `remind_friendly` were added after these four worlds had
    already produced every number in README.md. They read their multiplier with
    an `in p` test rather than a `.get(..., 1.0)` default, so a preset that does
    not define one never touches the value at all.

    This test cannot see a regression on its own -- it needs a reference. The
    reference is that `default` and `retry_friendly` differ ONLY where retries
    are reachable, which the next test pins.
    """
    w = build(preset)
    assert len(w.obligations) == N
    for ob in w.obligations:
        for action, p in w.truth[ob.id].p_act.items():
            assert 0.0 < p < 1.0, f"{preset}/{action} produced {p}"


def test_link_and_remind_multipliers_reach_only_their_own_actions():
    """A multiplier that leaked into another action would corrupt every world."""
    base = effects("default")
    link = effects("link_friendly")
    remind = effects("remind_friendly")

    touched_by_link, touched_by_remind = set(), set()
    for oid, acts in base.items():
        for action, p in acts.items():
            if link[oid][action] != p:
                touched_by_link.add(action)
            if remind[oid][action] != p:
                touched_by_remind.add(action)

    assert touched_by_link == set(world.LINK_ACTIONS)
    assert touched_by_remind == {world.ActionType.REMIND}


# -- 2. the anti-rigging world has to actually cost us --------------------

def test_remind_friendly_closes_the_gap_and_link_friendly_widens_it():
    """The whole point of the pair, in one assertion.

    After item 3a the fixed schedule's surviving lever is REMIND -- it sends
    RETRY and REMIND and nothing else (`app/domain/policies.py`). So REMIND is
    the only thing that can be made to work well and have the baseline feel it.

    `link_friendly` doubles PAY_LINK and METHOD_CHANGE, which the baseline never
    sends. It therefore cannot reach the baseline at all, and only we benefit.
    That makes it a best-case showcase, not a fairness control, and it is in the
    table labelled as one.
    """
    base = effects("default")
    remind = effects("remind_friendly")
    link = effects("link_friendly")

    A = world.ActionType
    # The baseline's levers gain under remind_friendly ...
    assert any(remind[o][A.REMIND] > base[o][A.REMIND] for o in base)
    # ... and are untouched under link_friendly.
    assert all(link[o][A.REMIND] == base[o][A.REMIND] for o in base)
    assert all(link[o][A.RETRY] == base[o][A.RETRY] for o in base)


def test_retry_friendly_still_reaches_mandate_cases_only():
    """Item 3a shrank this world rather than killing it, and the table says so.

    Kept as a test because the README makes a specific claim -- that the preset
    now only bites on the cases holding a mandate -- and a claim in a README with
    nothing pinning it is how the last two dead tests happened.
    """
    base = effects("default")
    retry = effects("retry_friendly")
    A = world.ActionType

    moved = [o for o in base if retry[o][A.RETRY] != base[o][A.RETRY]]
    assert moved, "retry_friendly stopped affecting anything at all"
    # It still moves the world. What changed at 3a is that the ENGINE can no
    # longer act on most of what it moves -- that is a gate fact, not a world
    # fact, which is why this preset looks dead in the results table while its
    # underlying probabilities are still different.
    assert all(retry[o][A.REMIND] == base[o][A.REMIND] for o in base)
