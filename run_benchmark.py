#!/usr/bin/env python3
"""
Four arms, same cases, same seed.

    python run_benchmark.py --n 5000 --seed 42 --preset default
    python run_benchmark.py --all-presets
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.config_loader import load_config
from app.repos import store
from sim.recorder import Recorder
from app.metrics import fmt, scoreboard
from app.domain.models import Arm
from app.services.bandit import Posterior
from sim.runner import run_arm
from sim.world import PRESETS, generate


def run_once(n: int, seed: int, preset: str, cfg, con=None, sample: int = 300) -> dict:
    world = generate(n, seed=seed, preset=preset,
                     window_hours=cfg.window_hours, reversal_rate=cfg.reversal_rate)
    posterior = Posterior(cfg)
    run_id = preset

    sample_ids = {ob.id for ob in world.obligations[:sample]}
    recorders = {}

    results, per_case = {}, {}
    # CONTROL first: the holdout is what teaches the engine its do-nothing baseline.
    for arm in (Arm.CONTROL, Arm.BASELINE, Arm.ENGINE, Arm.ORACLE):
        rec = Recorder(run_id, sample_ids) if con is not None else None
        recorders[arm.value] = rec
        results[arm.value], per_case[arm.value] = run_arm(
            world, arm, cfg, posterior, seed=seed, recorder=rec)

    board = scoreboard(results, per_case, n_cases=n)
    board["preset"] = preset
    board["n"] = n
    board["seed"] = seed
    board["posterior_sample"] = posterior.table(min_count=20)[:25]
    board["run_id"] = run_id

    if con is not None:
        store.reset_run(con, run_id)
        for rec in recorders.values():
            if rec:
                store.save_cases(con, rec.cases)
                store.save_decisions(con, rec.decisions)
        store.save_run(con, run_id, preset, n, seed, datetime.now().isoformat(), board)
    return board


def show(board: dict) -> None:
    h = board.get("headline", {})
    print(f"\n{'=' * 74}\n  preset: {board['preset']}   n={board['n']}   seed={board['seed']}\n{'=' * 74}")
    print(f"  {'arm':<10}{'recovered':>16}{'rate':>9}{'contacts':>11}{'actions':>10}{'written off':>14}")
    for name in ("CONTROL", "BASELINE", "ENGINE", "ORACLE"):
        a = board["arms"].get(name)
        if not a:
            continue
        print(f"  {name:<10}{fmt(a['gross_recovered']):>16}{a['recovery_rate'] * 100:>8.1f}%"
              f"{a['contacts_sent']:>11,}{a['actions_taken']:>10,}{a['written_off_count']:>14,}")
    print(f"\n  at risk            {fmt(h.get('at_risk'))}")
    print(f"  organic (control)  {fmt(h.get('control_organic'))}   <- money that arrived anyway")
    print(f"  INCREMENTAL        {fmt(h.get('incremental'))}")
    print(f"  net of reversals   {fmt(h.get('incremental_net'))}")
    print(f"    case-level CI      [{fmt(h.get('incremental_ci_low'))} .. {fmt(h.get('incremental_ci_high'))}]"
          f"   (bootstrap within this run)")
    if h.get("pct_of_oracle_ceiling") is not None:
        print(f"  % of oracle ceiling{h['pct_of_oracle_ceiling'] * 100:>7.1f}%")
    hon = board.get("honesty", {})
    print(f"\n  false chase /10k   engine {hon.get('false_chase_per_10k_engine')}"
          f"   baseline {hon.get('false_chase_per_10k_baseline')}")
    print(f"  left alone         {hon.get('left_alone_count'):,} cases"
          f"  ({fmt(hon.get('left_alone_value'))} deliberately not chased)")
    print(f"  written off        {hon.get('written_off_count'):,} cases"
          f"  ({fmt(hon.get('written_off_value'))})")
    if hon.get("where_we_lost"):
        print("  where we lost      " + ", ".join(
            f"{r['segment']} ({r['delta'] * 100:+.1f}pp)" for r in hon["where_we_lost"][:4]))
    print()


def sweep(n: int, seeds: list[int], preset: str, cfg) -> dict:
    """The full four-arm benchmark once per seed.

    The bootstrap CI resamples cases inside a single run, so it measures
    case-level variance only -- it cannot see the variance from redrawing the
    world and every action roll. Running the whole experiment per seed does.
    Report both; the seed-to-seed range is usually the wider and more honest one.
    """
    rows = []
    for s in seeds:
        b = run_once(n, s, preset, cfg, con=None)
        h = b["headline"]
        rows.append({
            "seed": s,
            "incremental": h["incremental"],
            "incremental_net": h["incremental_net"],
            "pct_of_oracle_ceiling": h["pct_of_oracle_ceiling"],
            "control_gross": h["control_organic"],
            "baseline_gross": h["baseline_gross"],
            "engine_gross": h["engine_gross"],
            "oracle_gross": h["oracle_gross"],
            "ci_low": h["incremental_ci_low"],
            "ci_high": h["incremental_ci_high"],
        })
        print(f"  seed {s} done   incremental {fmt(h['incremental'])}")

    inc = [r["incremental"] for r in rows]
    pct = [r["pct_of_oracle_ceiling"] for r in rows if r["pct_of_oracle_ceiling"] is not None]
    mean_inc = sum(inc) / len(inc)
    # Mean of the per-run case-level CIs, for side-by-side comparison with the
    # seed range. Averaging bounds is not a pooled interval and is not claimed as one.
    mean_ci = (sum(r["ci_low"] for r in rows) / len(rows),
               sum(r["ci_high"] for r in rows) / len(rows))
    return {
        "preset": preset, "n": n, "seeds": seeds, "runs": rows,
        "incremental_mean": round(mean_inc),
        "incremental_min": min(inc),
        "incremental_max": max(inc),
        "pct_of_oracle_mean": round(sum(pct) / len(pct), 4) if pct else None,
        "pct_of_oracle_min": min(pct) if pct else None,
        "pct_of_oracle_max": max(pct) if pct else None,
        "mean_case_level_ci_low": round(mean_ci[0]),
        "mean_case_level_ci_high": round(mean_ci[1]),
    }


def show_sweep(sw: dict) -> None:
    print(f"\n{'=' * 74}\n  SEED SWEEP   preset: {sw['preset']}   n={sw['n']}"
          f"   seeds={','.join(str(s) for s in sw['seeds'])}\n{'=' * 74}")
    print(f"  {'seed':<8}{'incremental':>16}{'% of ceiling':>15}")
    for r in sw["runs"]:
        pct = r["pct_of_oracle_ceiling"]
        print(f"  {r['seed']:<8}{fmt(r['incremental']):>16}"
              f"{(f'{pct * 100:.1f}%' if pct is not None else '-'):>15}")
    print(f"\n  mean incremental      {fmt(sw['incremental_mean'])}")
    print(f"  seed-to-seed range    [{fmt(sw['incremental_min'])} .. {fmt(sw['incremental_max'])}]"
          f"   <- across {len(sw['seeds'])} independent runs")
    print(f"  case-level CI (mean)  [{fmt(sw['mean_case_level_ci_low'])} .. "
          f"{fmt(sw['mean_case_level_ci_high'])}]   <- bootstrap within one run")
    if sw["pct_of_oracle_mean"] is not None:
        print(f"  % of oracle ceiling   {sw['pct_of_oracle_mean'] * 100:.1f}%"
              f"   [{sw['pct_of_oracle_min'] * 100:.1f}% .. {sw['pct_of_oracle_max'] * 100:.1f}%]")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--preset", default="default")
    ap.add_argument("--all-presets", action="store_true")
    ap.add_argument("--seeds", default=None,
                    help="comma-separated, e.g. 42,43,44,45,46 -- full benchmark per seed")
    ap.add_argument("--out", default="results.json")
    args = ap.parse_args()

    cfg = load_config()
    presets = list(PRESETS) if args.all_presets else [args.preset]

    if args.seeds:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        out = {}
        for p in presets:
            sw = sweep(args.n, seeds, p, cfg)
            out[p] = sw
            show_sweep(sw)
        with open("results_seeds.json", "w") as f:
            json.dump(out, f, indent=2, default=str)
        print("wrote results_seeds.json\n")
        return

    con = store.connect()
    store.init(con)

    boards = {}
    for p in presets:
        b = run_once(args.n, args.seed, p, cfg, con=con)
        boards[p] = b
        show(b)

    with open(args.out, "w") as f:
        json.dump(boards, f, indent=2, default=str)
    print(f"wrote {args.out}\n")


if __name__ == "__main__":
    main()
