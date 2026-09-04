"""
Beta-posterior contextual bandit with Thompson sampling.

Deliberately not a neural net. With no real data and hours on the clock, a table
of Beta distributions is better engineering than a model you cannot inspect:

  - uncertainty is free (a Beta is a shape, not a point)
  - exploration is free (sample instead of argmax -> Thompson sampling)
  - it is printable, so every claim about what the engine "learned" is checkable
  - it learns online, from the first case, with no training step

ActionType.NONE is tracked like any other arm. It is the do-nothing
counterfactual, and without it uplift cannot be computed at all.
"""
from __future__ import annotations

from collections import defaultdict

from app.domain.models import ActionType, Config


def parents_of(segment: str) -> list[str]:
    """failure|band|attempts|method -> progressively coarser buckets."""
    parts = segment.split("|")
    if len(parts) != 4:
        return ["*|*|*|*"]
    f, b, a, m = parts
    return [f"{f}|{b}|*|{m}", f"{f}|{b}|*|*", f"{f}|*|*|*", "*|*|*|*"]


class Posterior:
    def __init__(self, cfg: Config):
        self.prior_a = cfg.prior_alpha
        self.prior_b = cfg.prior_beta
        self.k = cfg.shrinkage_threshold
        self._t: dict[tuple[str, str], list[float]] = defaultdict(
            lambda: [self.prior_a, self.prior_b]
        )

    # -- reads ------------------------------------------------------------

    def _raw(self, segment: str, action: ActionType) -> tuple[float, float]:
        a, b = self._t[(segment, action.value)]
        return a, b

    def _mean_at(self, segment: str, action: ActionType) -> float:
        a, b = self._raw(segment, action)
        return a / (a + b)

    def params(self, segment: str, action: ActionType) -> tuple[float, float]:
        """Empirical-Bayes shrinkage: a thin bucket borrows from its parent."""
        a, b = self._raw(segment, action)
        n = (a - self.prior_a) + (b - self.prior_b)
        if n >= self.k:
            return a, b

        strength = self.k - n
        for parent in parents_of(segment):
            pa, pb = self._raw(parent, action)
            pn = (pa - self.prior_a) + (pb - self.prior_b)
            if pn > 0:
                pm = pa / (pa + pb)
                return a + pm * strength, b + (1.0 - pm) * strength
        return a, b

    def sample(self, segment: str, action: ActionType, rng) -> float:
        """Thompson draw. Uncertain -> explores. Confident -> exploits. Free."""
        a, b = self.params(segment, action)
        return float(rng.beta(a, b))

    def mean(self, segment: str, action: ActionType) -> float:
        a, b = self.params(segment, action)
        return a / (a + b)

    def count(self, segment: str, action: ActionType) -> int:
        a, b = self._raw(segment, action)
        return int(round((a - self.prior_a) + (b - self.prior_b)))

    # -- writes -----------------------------------------------------------

    def update(self, segment: str, action: ActionType, success: bool) -> None:
        """Update the exact bucket AND every parent, so coarse buckets stay warm."""
        for key in [segment, *parents_of(segment)]:
            cell = self._t[(key, action.value)]
            cell[0 if success else 1] += 1.0

    # -- inspection (this is what you show a judge) -----------------------

    def table(self, min_count: int = 1) -> list[dict]:
        rows = []
        for (segment, action), (a, b) in sorted(self._t.items()):
            n = int(round((a - self.prior_a) + (b - self.prior_b)))
            if n >= min_count:
                rows.append({
                    "segment": segment, "action": action, "n": n,
                    "mean": round(a / (a + b), 4), "alpha": round(a, 1), "beta": round(b, 1),
                })
        rows.sort(key=lambda r: (r["segment"], -r["mean"]))
        return rows

    def to_dict(self) -> dict[str, list[float]]:
        return {f"{s}::{a}": v for (s, a), v in self._t.items()}

    def load(self, d: dict[str, list[float]]) -> None:
        for key, v in d.items():
            s, a = key.split("::")
            self._t[(s, a)] = list(v)


class FixedPosterior:
    """Phase-2 stand-in: a hand-written lookup table, no learning.

    Lets the four-arm benchmark run before the bandit exists. Keep it -- it is
    also the fallback if the bandit misbehaves an hour before submission.
    """

    def __init__(self, table: dict[str, dict[str, float]], default: float = 0.05):
        self.table = table
        self.default = default

    def _p(self, segment: str, action: ActionType) -> float:
        failure = segment.split("|")[0]
        return self.table.get(failure, {}).get(action.value, self.default)

    def sample(self, segment, action, rng):
        return self._p(segment, action)

    def mean(self, segment, action):
        return self._p(segment, action)

    def count(self, segment, action):
        return 0

    def update(self, segment, action, success):
        pass

    def table_rows(self):
        return []


class FrozenPosterior:
    """Replay support: returns the probabilities recorded at decision time.

    Replay must answer "given what we knew AND what the model believed then,
    would we decide the same?" -- so it cannot use today's posterior. Storing the
    whole table per decision would be enormous; storing the handful of
    probabilities that actually fed the choice is exact and nearly free.
    """

    def __init__(self, candidates: list[dict]):
        self._p = {c["action"]: c["p_act"] for c in candidates}
        self._none = candidates[0]["p_none"] if candidates else 0.0

    def sample(self, segment, action, rng=None):
        return self.mean(segment, action)

    def mean(self, segment, action):
        key = action.value if hasattr(action, "value") else str(action)
        if key == "NONE":
            return self._none
        return self._p.get(key, 0.0)

    def count(self, segment, action):
        return 0

    def update(self, segment, action, success):
        pass
