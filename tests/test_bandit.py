"""
The bandit's memory: what it forgets, and the one thing it must never forget.

Item 3c added `bandit.decay`. What is pinned here:

  decay off is arithmetically identical to plain counting -- the feature can be
    turned off and the old behaviour is exactly recovered, not approximately
  a regime change IS learned with decay and is NOT learned without it. This is
    the whole reason the knob exists, and the benchmark cannot show it: seven
    simulated days is far too short for issuer behaviour to drift
  THE DO-NOTHING COUNTERFACTUAL SURVIVES A FULL ENGINE ARM. `ActionType.NONE` is
    the denominator of every uplift. `run_benchmark.py` runs the arms
    sequentially, so CONTROL feeds ~2000 NONE observations in one burst and then
    ENGINE takes ~3200 actions without touching NONE again. Any decay scheme
    that ages a cell for observations landing in OTHER cells eats that burst --
    global decay leaves 53 of the 2000 alive, with a mean that still looks
    perfectly reasonable. `test_the_counterfactual_survives_a_full_engine_arm`
    is what stops someone "fixing" per-cell decay into that.
  the stated limitation is real and deliberate: a cell only ages when it is
    used. Pinned so it stays a documented property rather than a surprise.
"""
from __future__ import annotations

from app.config_loader import load_config
from app.domain.models import ActionType
from app.services.bandit import Posterior

SEG = "INSUFFICIENT_FUNDS|mid|1|card"
ON = load_config()
OFF = load_config(**{"bandit.decay": 1.0})


def feed(post, n, rate_denom, action=ActionType.REMIND, segment=SEG):
    """`n` deterministic observations at 1-in-`rate_denom` success.

    Deterministic, not sampled: a decay test that also has to survive sampling
    noise is two tests wearing one name, and the flaky one wins eventually.
    """
    for i in range(n):
        post.update(segment, action, i % rate_denom == 0)


# -- the knob is real, and turning it off recovers exactly the old behaviour --

def test_the_config_carries_the_decay():
    assert ON.bandit_decay == 0.999
    assert OFF.bandit_decay == 1.0


def test_decay_off_is_arithmetically_identical_to_plain_counting():
    """Not "close to". Identical. `if d < 1.0` skips the arithmetic entirely, so
    a config with decay off cannot drift from the pre-3c posterior by a float
    epsilon that compounds over 2000 updates."""
    post = Posterior(OFF)
    feed(post, 500, 4)
    a, b = post._raw(SEG, ActionType.REMIND)
    assert (a, b) == (OFF.prior_alpha + 125, OFF.prior_beta + 375)


# -- the reason the knob exists ------------------------------------------

def test_a_regime_change_is_learned_only_with_decay():
    """80% for 3000 observations, then 10% for 3000. What does the model believe?

    This is the claim decay is making, and it is not a claim the benchmark can
    check -- the simulated world's probabilities are fixed for its whole seven
    days, so nothing there ever goes stale. Here the truth moves and the two
    posteriors are asked what they think.
    """
    decayed, plain = Posterior(ON), Posterior(OFF)
    for post in (decayed, plain):
        for i in range(3000):
            post.update(SEG, ActionType.REMIND, i % 5 != 0)      # 80% succeed
        for i in range(3000):
            post.update(SEG, ActionType.REMIND, i % 10 == 0)     # then 10%

    d, p = decayed.mean(SEG, ActionType.REMIND), plain.mean(SEG, ActionType.REMIND)
    assert 0.10 <= d <= 0.20, f"decay did not follow the regime change: {d}"
    assert p >= 0.40, f"no-decay should still be anchored to the dead 80%: {p}"
    assert p - d > 0.25, "the two posteriors have not meaningfully diverged"


def test_evidence_saturates_at_the_memory_length():
    """A cell cannot accumulate unbounded confidence. 1/(1-0.999) = 1000, so
    10,000 observations leave about 1000 alive -- which is the difference between
    "certain because it is true" and "certain because it is old"."""
    post = Posterior(ON)
    feed(post, 10_000, 3)
    n = post.count(SEG, ActionType.REMIND)
    assert 950 <= n <= 1000, n


def test_count_reports_decayed_evidence_not_arrivals():
    post = Posterior(ON)
    feed(post, 5000, 2)
    assert post.count(SEG, ActionType.REMIND) < 1100     # not 5000


def test_decay_relaxes_toward_the_prior_and_never_past_it():
    """Decaying raw alpha would walk it to zero, and Beta(0, b) is not a
    distribution -- `rng.beta` would raise mid-benchmark. Decaying the excess
    over the prior means an unsupported belief lands back on the prior, which is
    where a belief you cannot evidence belongs."""
    post = Posterior(ON)
    feed(post, 2000, 1)                                   # every one a success
    hot = post.mean(SEG, ActionType.REMIND)
    assert hot > 0.99
    for _ in range(20_000):                               # then nothing but failure
        post.update(SEG, ActionType.REMIND, False)
    a, b = post._raw(SEG, ActionType.REMIND)
    assert a >= ON.prior_alpha, "alpha fell below the prior floor"
    assert a - ON.prior_alpha < 1e-4, "old evidence never died"   # was ~1000
    assert post.mean(SEG, ActionType.REMIND) < 0.01


# -- the one that must not be "improved" ----------------------------------

def test_the_counterfactual_survives_a_full_engine_arm():
    """The shape of `run_benchmark.py`, in miniature.

    CONTROL dumps its NONE observations in one burst; ENGINE then runs a whole
    arm of contact actions without ever touching NONE again. p_none must mean the
    same thing at the end of that as it did at the start, because every uplift in
    the run is measured against it.

    Global decay fails this at n=53 out of 2000 -- with a mean of 0.2926 against
    the true 0.2887, which is to say it fails it invisibly if you only read means.
    So this asserts on BOTH.
    """
    post = Posterior(ON)
    segs = [f"INSUFFICIENT_FUNDS|{band}|{att}|card" for band in ("lo", "mid", "hi")
            for att in (1, 2, 3)]
    for i in range(2000):                                  # <- the CONTROL burst
        post.update(segs[i % len(segs)], ActionType.NONE, i % 10 < 3)
    before_n = post.count("*|*|*|*", ActionType.NONE)
    before_mean = post.mean("*|*|*|*", ActionType.NONE)

    for i in range(3200):                                  # <- the whole ENGINE arm
        act = (ActionType.REMIND, ActionType.PAY_LINK, ActionType.METHOD_CHANGE)[i % 3]
        post.update(segs[i % len(segs)], act, i % 5 == 0)

    assert post.count("*|*|*|*", ActionType.NONE) == before_n, (
        "an observation in another cell aged the do-nothing counterfactual. "
        "That is global decay, and it ends with uplift measured against 53 "
        "surviving observations -- see the bandit module docstring.")
    assert post.mean("*|*|*|*", ActionType.NONE) == before_mean


def test_a_cell_only_ages_when_it_is_used():
    """The stated limitation, pinned so it cannot become a surprise.

    An action the engine stops trying keeps its last belief instead of relaxing
    to the prior and being re-explored. This is the conservative direction --
    the model never invents uncertainty it has no evidence for -- but it does
    mean `decay` is recency-weighting and not staleness-detection, and the
    README says so rather than implying the stronger property.
    """
    post = Posterior(ON)
    feed(post, 400, 2, action=ActionType.RETRY)
    frozen = post._raw(SEG, ActionType.RETRY)
    feed(post, 5000, 3, action=ActionType.REMIND)
    assert post._raw(SEG, ActionType.RETRY) == frozen


# -- decay and shrinkage still compose ------------------------------------

def test_a_thin_bucket_still_borrows_from_a_decayed_parent():
    """Shrinkage compares against DECAYED n, so a parent that has gone quiet
    stops lending as much weight -- which is correct, and worth checking because
    the two features touch the same numbers from opposite ends."""
    post = Posterior(ON)
    for i in range(1000):
        post.update("NETWORK_ERROR|mid|1|upi", ActionType.REMIND, True)
    thin = "NETWORK_ERROR|mid|9|upi"                        # same parents, no data
    post.update(thin, ActionType.REMIND, True)
    assert post.count(thin, ActionType.REMIND) == 1
    assert post.mean(thin, ActionType.REMIND) > 0.9         # borrowed the parent
