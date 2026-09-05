"""
The scoreboard maths.

The headline is not "we recovered X". It is "we recovered X MORE THAN THE
CONTROL ARM, plus or minus this much" -- and the interval matters as much as the
number, because without it a difference of any size is unfalsifiable.
"""
from __future__ import annotations

import numpy as np


def bootstrap_ci(engine_vals, control_vals, n_boot: int = 1000, seed: int = 7,
                 alpha: float = 0.05) -> tuple[float, float]:
    """95% CI on the difference in mean recovered value per case."""
    rng = np.random.default_rng(seed)
    e = np.asarray(engine_vals, dtype=float)
    c = np.asarray(control_vals, dtype=float)
    if len(e) == 0 or len(c) == 0:
        return (0.0, 0.0)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        diffs[i] = (rng.choice(e, len(e), replace=True).mean()
                    - rng.choice(c, len(c), replace=True).mean())
    lo, hi = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return float(centre - half), float(centre + half)


def scoreboard(results: dict, per_case: dict | None = None, n_cases: int = 0) -> dict:
    """`results` maps arm name -> ArmResult. `per_case` maps arm -> list of recovered paise."""
    control = results.get("CONTROL")
    engine = results.get("ENGINE")
    baseline = results.get("BASELINE")
    oracle = results.get("ORACLE")

    out: dict = {"arms": {k: v.as_dict() for k, v in results.items()}}
    if not (control and engine):
        return out

    incremental = engine.gross_recovered - control.gross_recovered
    incremental_net = engine.net_recovered - control.net_recovered
    vs_baseline = engine.gross_recovered - (baseline.gross_recovered if baseline else 0)

    ceiling = (oracle.gross_recovered - control.gross_recovered) if oracle else 0
    pct_of_oracle = (incremental / ceiling) if ceiling > 0 else None

    ci = (0.0, 0.0)
    if per_case:
        lo, hi = bootstrap_ci(per_case.get("ENGINE", []), per_case.get("CONTROL", []))
        n = n_cases or len(per_case.get("ENGINE", []))
        ci = (lo * n, hi * n)

    lost = []
    for seg, e in engine.by_segment.items():
        c = control.by_segment.get(seg)
        if not c or c["at_risk"] == 0 or e["at_risk"] == 0:
            continue
        er, cr = e["recovered"] / e["at_risk"], c["recovered"] / c["at_risk"]
        if er < cr:
            lost.append({"segment": seg, "engine_rate": round(er, 4),
                         "control_rate": round(cr, 4), "delta": round(er - cr, 4),
                         "n": e["n"]})
    lost.sort(key=lambda r: r["delta"])

    out["headline"] = {
        "at_risk": engine.at_risk,
        "control_organic": control.gross_recovered,
        "engine_gross": engine.gross_recovered,
        "baseline_gross": baseline.gross_recovered if baseline else None,
        "oracle_gross": oracle.gross_recovered if oracle else None,
        "incremental": incremental,
        "incremental_net": incremental_net,
        "incremental_ci_low": round(ci[0]),
        "incremental_ci_high": round(ci[1]),
        "incremental_vs_baseline": vs_baseline,
        "pct_of_oracle_ceiling": round(pct_of_oracle, 4) if pct_of_oracle is not None else None,
    }
    out["honesty"] = {
        "false_chase_per_10k_engine": round(10_000 * engine.false_chases / max(1, engine.cases), 2),
        "false_chase_per_10k_baseline": round(
            10_000 * baseline.false_chases / max(1, baseline.cases), 2) if baseline else None,
        # Item 3d. This used to report `engine.double_charges`, which is 0 -- and it
        # is 0 because NOTHING IN THE CODEBASE EVER INCREMENTS IT. There is no
        # server-initiated debit to double: `RazorpayExecutor` records RETRY as
        # INTENT_ONLY and the simulator has no debit path either, so no event
        # exists that could raise the count. A safety metric reading 0 says "we
        # prevented this"; this one measured nothing at all. Stated instead.
        #
        # Contrast `false_chase_per_10k_engine`, which is also 0 for the engine:
        # that counter has live increment sites and the baseline arm scores 145.0
        # on the same run, which is what makes the engine's 0 a result rather than
        # an absence.
        "double_charges": None,
        "double_charges_status": "not applicable -- no live debits are issued",
        "double_charges_why": (
            "RETRY is INTENT_ONLY: RazorpayExecutor never sends a server-initiated "
            "debit, so there is no charge that could be issued twice. This is not a "
            "prevented-zero, it is an inapplicable metric, and it is reported that "
            "way rather than as a 0 that looks like a win."),
        "left_alone_count": engine.left_alone_count,
        "left_alone_value": engine.left_alone_value,
        "written_off_count": engine.written_off_count,
        "written_off_value": engine.written_off_value,
        "contacts_engine": engine.contacts_sent,
        "contacts_baseline": baseline.contacts_sent if baseline else None,
        "where_we_lost": lost[:8],
    }
    return out


def fmt(paise: int | float | None) -> str:
    if paise is None:
        return "-"
    return f"Rs {paise / 100:,.0f}"
