"""YAML -> the frozen Config the pure domain consumes. Loader lives outside domain/."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.domain.models import Config

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
_CACHE: dict[str, Config] = {}


def load_config(path: str | Path = DEFAULT_PATH, **overrides: Any) -> Config:
    """The YAML, optionally overlaid with dotted-path overrides.

    CORRECTNESS: an overridden config gets its OWN version string. `_CACHE` is
    keyed on `cfg.version` and `version()` below is what `app/api/replay.py` uses
    to rebuild the config a decision was made under. Every load from
    `default.yaml` reports version "v1", so without a suffix a single
    `load_config(**{"compliance.quiet_hours.start": 0})` -- which is exactly what
    a merchant quiet-hours override is -- would overwrite the cached "v1" and
    replay would silently re-decide old cases under settings that did not exist
    when they were decided. Pinned by `tests/test_ops.py`.

    The suffix is a digest of the overrides, so the same overlay always produces
    the same version and a stored `config_version` stays resolvable.
    """
    raw = yaml.safe_load(Path(path).read_text())
    for k, v in overrides.items():
        _set_path(raw, k, v)
    suffix = _suffix(overrides)

    e, r, c, a, b, t, run = (raw["experiment"], raw["recovery"], raw["compliance"],
                             raw["allocator"], raw["bandit"], raw["timing"], raw["runtime"])

    cfg = Config(
        version=raw.get("version", "v1") + suffix,
        holdout_pct=e["holdout_pct"], seed=e["seed"],
        reversal_rate=e["reversal_rate"],
        attribution_window_days=e["attribution_window_days"],
        window_hours=r["window_hours"], max_attempts=r["max_attempts"],
        max_rung=r["max_rung"], max_contacts_7d=r["max_contacts_7d"],
        afa_limit=c["afa_limit"], afa_limit_essentials=c["afa_limit_essentials"],
        mandate_pre_debit_notice_hours=c["mandate_pre_debit_notice_hours"],
        quiet_start=c["quiet_hours"]["start"], quiet_end=c["quiet_hours"]["end"],
        respect_dnd=c["respect_dnd"],
        global_contact_budget_per_tick=a["global_contact_budget_per_tick"],
        action_cost=raw["action_cost"], friction_cost=raw["friction_cost"],
        contacts_used=raw["contacts_used"],
        prior_alpha=b["prior_alpha"], prior_beta=b["prior_beta"],
        shrinkage_threshold=b["shrinkage_threshold"],
        baseline_retry_schedule_hours=tuple(raw["baseline"]["retry_schedule_hours"]),
        payday_window_days=tuple(t["payday_window_days"]),
        insufficient_funds_wait_for_payday=t["insufficient_funds_wait_for_payday"],
        downtime_backoff_minutes=t["downtime_backoff_minutes"],
        dry_run=run["dry_run"], batch_size=run["batch_size"],
        raw=raw,
    )
    _CACHE[cfg.version] = cfg
    return cfg


def version(v: str) -> Config:
    """Replay needs the config as it was when the decision was made."""
    return _CACHE.get(v) or load_config()


def _suffix(overrides: dict[str, Any]) -> str:
    """"" for the plain config, "+abc123" for an overlay. Empty is load-bearing:
    it is what keeps the benchmark's config_version -- and therefore its output --
    byte-identical to what it was before overrides existed."""
    if not overrides:
        return ""
    blob = json.dumps(sorted(overrides.items()), default=str, sort_keys=True)
    return "+" + hashlib.sha1(blob.encode()).hexdigest()[:6]


def _set_path(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value
