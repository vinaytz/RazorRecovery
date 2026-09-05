"""
Operator control. The stop button, and everything an operator needs to trust it.

Three ideas, and the ordering between them is the whole design:

1. PAUSE STOPS DECIDING AND ACTING. IT DOES NOT STOP INGESTING.
   A recovery engine that stops listening when it is paused comes back to a
   ledger that has silently drifted: payments landed, checkouts were abandoned,
   webhooks were retried and dropped. The single most dangerous moment for a
   money system is the minute after somebody hits stop, and it is dangerous
   precisely because the temptation is to stop everything. So: webhooks are
   still accepted, settlements are still matched, cases are still opened, the
   abandonment sweep still runs. What stops is the engine choosing to spend
   money or contact a human.

2. PENDING ACTIONS ARE CANCELLED, NOT HELD.
   A queue of contacts drained the instant somebody resumes is not a pause, it
   is a delay with a cliff at the end. Cancelling costs the rung that was
   already climbed -- the ladder does not descend -- and that cost is shown on
   the tab rather than hidden.

3. NOTHING HERE IS READ BY THE BENCHMARK.
   Every function takes a live sqlite connection. `sim/` never opens one and
   `run_benchmark.py` never calls this module, so no operator setting can move
   a benchmark number. `app/domain/` is untouched: quiet hours are still
   enforced by G12 reading `cfg.quiet_start`/`cfg.quiet_end`, and all this does
   is decide WHICH config the live worker hands it.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.config_loader import load_config
from app.repos import store

log = logging.getLogger("razorrecovery.ops")

GLOBAL, MERCHANT, ACTION, RUNG = "global", "merchant", "action", "rung"
SCOPES = (GLOBAL, MERCHANT, ACTION, RUNG)

# Settings keys. JSON values, one row each.
K_DRY_RUN = "dry_run"                 # true | false | null (null = follow env)
K_QUIET = "quiet_hours"               # {merchant_id: {"start": int, "end": int}}

# How long an unresolved downtime may sit before the attention row changes state.
# NOT an expiry. See `stale_downtime_hours`.
DEFAULT_STALE_DOWNTIME_HOURS = 6


def stale_downtime_hours() -> float:
    """`STALE_DOWNTIME_HOURS`. When an open downtime starts shouting. Default 6.

    THIS IS A THRESHOLD, NOT A TIMER. Crossing it changes the colour and the copy
    of an Ops row and nothing else: the `downtimes` row stays `started`, G9 keeps
    holding that method, and no case moves. Auto-expiring at N hours is the
    end-time guess item 3b exists to refuse -- it would have this system decide an
    outage was over with no evidence and send customers at a dead rail silently.

    The threshold exists because the failure it catches is silent and slow. A
    dropped `.resolved` holds every case on that method until `window_hours` closes
    and writes it off, so it surfaces as a rise in write-offs on one method a week
    later. An attention row only works if somebody is looking, and at hour 1 there
    is nothing to distinguish "issuer is genuinely down" from "we lost the
    webhook". By hour 6 there is: real Razorpay outages are minutes to a couple of
    hours, so six is generous for a real one and early for a lost one.

    Read per call, like `TIME_SCALE` and `ABANDON_MINUTES`, so a demo can set it to
    0 and show the STALLED row without waiting six hours. Anything unparseable or
    negative falls back to the default -- a bad value must not silence the alarm.
    """
    raw = os.environ.get("STALE_DOWNTIME_HOURS", "")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_STALE_DOWNTIME_HOURS)
    return v if v >= 0 else float(DEFAULT_STALE_DOWNTIME_HOURS)


def default_merchant() -> str:
    """The merchant this deployment serves.

    HONEST LIMIT: one merchant per process, from `MERCHANT_ID`. `LiveWorker`
    stamps every snapshot with it, so there is no per-case merchant to scope by
    yet. Pausing this merchant is therefore the same as pausing globally today,
    and the Ops tab says so rather than implying a multi-tenant control that has
    nothing behind it.
    """
    return os.getenv("MERCHANT_ID", "merchant_1")


# -- pauses ----------------------------------------------------------------

@dataclass(frozen=True)
class PauseSet:
    """The active pauses, resolved once per tick and asked many times."""

    rows: tuple[dict, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.rows)

    @property
    def is_global(self) -> bool:
        return any(r["scope"] == GLOBAL for r in self.rows)

    def _values(self, scope: str) -> set[str]:
        return {str(r["value"]) for r in self.rows if r["scope"] == scope and r["value"]}

    def blocks_case(self, merchant_id: str, rung: int) -> str | None:
        """Why this case may not be decided on, or None. A reason, never a bool."""
        if self.is_global:
            return "PAUSED_GLOBAL"
        if merchant_id in self._values(MERCHANT):
            return f"PAUSED_MERCHANT:{merchant_id}"
        for v in self._values(RUNG):
            try:
                if int(rung) >= int(v):
                    return f"PAUSED_RUNG>={v}"
            except (TypeError, ValueError):
                continue
        return None

    def blocks_action(self, action_type: str) -> str | None:
        if self.is_global:
            return "PAUSED_GLOBAL"
        if str(action_type).upper() in {v.upper() for v in self._values(ACTION)}:
            return f"PAUSED_ACTION:{action_type}"
        return None

    def as_json(self) -> list[dict]:
        return [dict(r) for r in self.rows]


def pause_set(con) -> PauseSet:
    return PauseSet(tuple(dict(r) for r in con.execute(
        "SELECT * FROM pauses WHERE lifted_at IS NULL ORDER BY id")))


def pause(con, scope: str = GLOBAL, value: str | None = None, *,
          reason: str = "", who: str = "operator",
          now: datetime | None = None) -> dict:
    """Raise a pause and cancel everything already queued inside it.

    Cancelling is the second half of the promise. Freezing new decisions while a
    PAY_LINK scheduled ninety seconds ago still fires is not a pause, and the
    operator who pressed the button would have no way to know.
    """
    scope = scope if scope in SCOPES else GLOBAL
    now = now or datetime.now()
    if scope == GLOBAL:
        value = None
    elif not value:
        raise ValueError(f"scope '{scope}' needs a value")

    existing = con.execute(
        "SELECT id FROM pauses WHERE scope = ? AND IFNULL(value,'') = IFNULL(?,'')"
        " AND lifted_at IS NULL", (scope, value)).fetchone()
    if existing:
        return {"ok": True, "already": True, "pause_id": existing[0],
                "cancelled": 0, "pauses": pause_set(con).as_json()}

    cur = con.execute(
        "INSERT INTO pauses (scope, value, reason, who, created_at, lifted_at,"
        " lifted_by, cancelled) VALUES (?,?,?,?,?,NULL,NULL,0)",
        (scope, value, reason or None, who, now.isoformat()))
    pid = int(cur.lastrowid or 0)
    killed = cancel_pending(con, scope, value, who)
    con.execute("UPDATE pauses SET cancelled = ? WHERE id = ?", (killed, pid))
    # What the audit row has to answer later is "what did stopping cost us", so
    # the count of cancelled actions is part of the record, not a log line.
    _audit(con, who, f"pause.{scope}", None,
           json.dumps({"scope": scope, "value": value or "everything",
                       "cancelled": killed}),
           reason or None, now)
    con.commit()
    log.warning("RECOVERY PAUSED scope=%s value=%s by=%s reason=%s -- %s pending "
                "action(s) cancelled. Ingestion continues.",
                scope, value, who, reason or "(none given)", killed)
    return {"ok": True, "already": False, "pause_id": pid, "cancelled": killed,
            "pauses": pause_set(con).as_json(),
            "note": "events, settlements and the abandonment sweep keep running"}


def resume(con, pause_id: int | None = None, *, who: str = "operator",
           now: datetime | None = None) -> dict:
    """Lift one pause, or every pause. The row stays; `lifted_at` is stamped."""
    now = now or datetime.now()
    if pause_id is None:
        cur = con.execute("UPDATE pauses SET lifted_at = ?, lifted_by = ?"
                          " WHERE lifted_at IS NULL", (now.isoformat(), who))
    else:
        cur = con.execute("UPDATE pauses SET lifted_at = ?, lifted_by = ?"
                          " WHERE id = ? AND lifted_at IS NULL",
                          (now.isoformat(), who, pause_id))
    n = cur.rowcount
    if n:
        _audit(con, who, "resume", None, json.dumps({"lifted": n}), None, now)
    con.commit()
    log.warning("RECOVERY RESUMED: %s pause(s) lifted by %s", n, who)
    return {"ok": True, "lifted": n, "pauses": pause_set(con).as_json(),
            "note": ("cases resume from the rung they had already climbed -- the "
                     "ladder never descends, so a pause costs whatever it cancelled")}


def cancel_pending(con, scope: str, value: str | None, who: str) -> int:
    """PENDING -> CANCELLED for everything inside the scope. Returns the count.

    IN_FLIGHT is deliberately left alone: that action is mid-call and its result
    is already unknown. Rewriting it here would be guessing, which is the one
    thing `app/controllers/execute.py` exists not to do.
    """
    detail = f"CANCELLED_BY_PAUSE ({scope}{':' + value if value else ''}) by {who}"
    base = "UPDATE actions SET status='CANCELLED', detail=? WHERE status='PENDING'"
    if scope == GLOBAL or (scope == MERCHANT and value == default_merchant()):
        cur = con.execute(base, (detail,))
    elif scope == ACTION:
        cur = con.execute(base + " AND type = ?", (detail, str(value).upper()))
    elif scope == RUNG:
        try:
            floor = int(str(value))
        except (TypeError, ValueError):
            return 0
        cur = con.execute(
            base + " AND case_id IN (SELECT case_id FROM cases WHERE rung >= ?)",
            (detail, floor))
    else:
        return 0
    con.commit()
    return cur.rowcount


# -- settings --------------------------------------------------------------

def get_setting(con, key: str, default: Any = None) -> Any:
    r = con.execute("SELECT value FROM ops_settings WHERE key = ?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return default


def set_setting(con, key: str, value: Any, *, who: str = "operator",
                note: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    before = con.execute("SELECT value FROM ops_settings WHERE key = ?", (key,)).fetchone()
    after = json.dumps(value)
    con.execute("INSERT OR REPLACE INTO ops_settings (key, value, changed_by, changed_at)"
                " VALUES (?,?,?,?)", (key, after, who, now.isoformat()))
    _audit(con, who, key, before[0] if before else None, after, note, now)
    con.commit()
    return {"key": key, "before": json.loads(before[0]) if before else None,
            "after": value, "who": who, "at": now.isoformat()}


def _audit(con, who: str, key: str, before: str | None, after: str | None,
           note: str | None, now: datetime) -> None:
    con.execute("INSERT INTO ops_audit (at, who, key, before, after, note)"
                " VALUES (?,?,?,?,?,?)",
                (now.isoformat(), who, key, before, after, note))


def audit(con, limit: int = 30) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM ops_audit ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        for k in ("before", "after"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, ValueError):
                    pass
        out.append(d)
    return out


# -- dry run <-> live ------------------------------------------------------

def effective_dry_run(con) -> dict:
    """Which mode we are actually in, and who decided it.

    The env default wins until an operator overrides it in this database, and the
    answer always says which of the two it was. "Are we live right now" is not a
    question anybody should have to answer by reading a shell history.
    """
    override = get_setting(con, K_DRY_RUN, None)
    env = _env_dry_run()
    if override is None:
        return {"dry_run": env, "source": "env", "env_default": env}
    return {"dry_run": bool(override), "source": "operator", "env_default": env}


def set_dry_run(con, value: bool | None, *, who: str = "operator",
                note: str | None = None) -> dict:
    """`None` clears the override and falls back to the environment."""
    out = set_setting(con, K_DRY_RUN, value, who=who, note=note)
    state = effective_dry_run(con)
    if not state["dry_run"]:
        log.warning("LIVE MODE ENABLED by %s -- real sends are now possible. %s",
                    who, note or "")
    else:
        log.warning("DRY RUN restored by %s -- nothing will leave the process.", who)
    return {**out, **state}


def _env_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")


# -- merchant-configurable quiet hours -------------------------------------
# G12 is unchanged and is NOT weakened. It reads `cfg.quiet_start`/`cfg.quiet_end`
# from whatever config it is handed; all that changes is that the live worker now
# hands it a config built for that merchant. A 24/7 gaming merchant and a B2B
# invoicing merchant genuinely have different windows, and the alternative --
# editing config/default.yaml -- would move the benchmark inputs.

_CFG_CACHE: dict[tuple[int, int], Any] = {}


def quiet_hours_all(con) -> dict:
    return get_setting(con, K_QUIET, {}) or {}


def quiet_hours(con, merchant_id: str | None = None) -> dict:
    """The window in force for a merchant, and where it came from."""
    merchant_id = merchant_id or default_merchant()
    base = load_config()
    over = quiet_hours_all(con).get(merchant_id)
    if not over:
        return {"merchant_id": merchant_id, "start": base.quiet_start,
                "end": base.quiet_end, "source": "config/default.yaml"}
    return {"merchant_id": merchant_id, "start": int(over["start"]),
            "end": int(over["end"]), "source": "operator"}


def set_quiet_hours(con, merchant_id: str, start: int, end: int, *,
                    who: str = "operator", note: str | None = None) -> dict:
    """Set a merchant's contact window. 0..23, and start == end means no window.

    Validation is a range check, not a policy: an operator may legitimately want
    0->0 (a 24/7 merchant) and refusing that would be the dashboard overruling
    the merchant. What it may NOT do is write something G12 cannot evaluate.
    """
    start, end = int(start), int(end)
    for v in (start, end):
        if not 0 <= v <= 23:
            raise ValueError(f"quiet hours must be 0..23, got {v}")
    all_ = dict(quiet_hours_all(con))
    all_[merchant_id] = {"start": start, "end": end}
    out = set_setting(con, K_QUIET, all_, who=who, note=note)
    log.warning("quiet hours for %s set to %02d:00-%02d:00 by %s",
                merchant_id, start, end, who)
    return {**out, **quiet_hours(con, merchant_id),
            "config_version": config_for(con, merchant_id).version}


def config_for(con, merchant_id: str | None = None):
    """The Config the live worker should hand `decide()` for this merchant.

    Returns the plain config when there is no override, so the common path is
    byte-identical to what it was before this module existed.
    """
    q = quiet_hours(con, merchant_id)
    if q["source"] != "operator":
        return load_config()
    key = (q["start"], q["end"])
    if key not in _CFG_CACHE:
        _CFG_CACHE[key] = load_config(**{"compliance.quiet_hours.start": q["start"],
                                         "compliance.quiet_hours.end": q["end"]})
    return _CFG_CACHE[key]


# -- what happened today ---------------------------------------------------

def today(con, now: datetime | None = None) -> dict:
    """Decisions, actions, contacts, recovered, budget used. Since local midnight.

    `budget_used` is real money: `config.action_cost` in paise for every action
    executed today. It is shown next to what was recovered because a recovery
    number with no cost next to it is a number nobody can act on.
    """
    now = now or datetime.now()
    since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cfg = load_config()

    def one(sql, *p):
        return int(con.execute(sql, p).fetchone()[0] or 0)

    by_type = {r["type"]: r["n"] for r in con.execute(
        "SELECT type, COUNT(*) n FROM actions WHERE created_at >= ?"
        " AND status IN ('DONE','FAILED','UNKNOWN','IN_FLIGHT') GROUP BY type", (since,))}
    spent = sum(int(cfg.action_cost.get(t, 0)) * n for t, n in by_type.items())
    recovered = one("SELECT COALESCE(SUM(amount),0) FROM settlements WHERE settled_at >= ?",
                    since)
    attributed = one("SELECT COALESCE(SUM(amount),0) FROM settlements"
                     " WHERE settled_at >= ? AND attributed = 1", since)
    return {
        "since": since,
        "decisions": one("SELECT COUNT(*) FROM decisions WHERE run_id='live'"
                         " AND decided_at >= ?", since),
        # The count of decisions is large and the count of cases is small, and
        # without both the first number reads as a lie. A waiting case is
        # re-decided every tick -- that is what WAIT means here: ask again in a
        # second -- so a handful of open cases produces thousands of rows a day.
        "cases_decided": one("SELECT COUNT(DISTINCT case_id) FROM decisions"
                             " WHERE run_id='live' AND decided_at >= ?", since),
        "actions_executed": sum(by_type.values()),
        "actions_by_type": by_type,
        "actions_cancelled": one("SELECT COUNT(*) FROM actions WHERE created_at >= ?"
                                 " AND status = 'CANCELLED'", since),
        "contacts": one("SELECT COUNT(*) FROM contacts WHERE sent_at >= ? AND ok = 1",
                        since),
        "contacts_failed": one("SELECT COUNT(*) FROM contacts WHERE sent_at >= ?"
                               " AND ok = 0", since),
        "recovered": recovered,
        "recovered_attributed": attributed,
        "budget_used": spent,
        "budget_note": ("cost of the actions taken, from config action_cost. "
                        "recovered is money that arrived; attributed is the part "
                        "we followed a contact with"),
        "contact_budget_per_tick": cfg.global_contact_budget_per_tick,
    }


# -- the attention list ----------------------------------------------------

def attention(con, now: datetime | None = None) -> dict:
    """The five things a human has to look at. Nothing here resolves itself.

    UNKNOWN actions and FAILED actions are separate rows on purpose. UNKNOWN
    means we do not know whether money moved and the reconciler can answer it.
    FAILED means we do know, and nobody was told -- which is the gap
    `docs/evidence/pre_3a_wasted_rung.txt` found: an INTENT_ONLY retry burns a
    rung and is silent everywhere except a column in this table.

    Open downtimes are here for the opposite reason to the others: most of them
    resolve themselves within minutes and need nobody. The row exists because
    nothing in this system ever expires one on a timer -- only Razorpay's
    `.resolved` clears it (item 3b, and see the `downtimes` DDL on why guessing an
    end time is the thing we refuse to do). A dropped resolve webhook therefore does
    not just block RETRY on that method: G9 sets `wait_until`, so the engine defers
    every open case on that method until `window_hours` closes and writes it off. It
    would surface a week later as write-offs on one method, not as an outage.
    `open_for_minutes` climbing past anything plausible is the only earlier signal.
    Deciding an outage is over is a human's call, made here, not an inference the
    engine makes quietly.

    Each row is OPEN or STALLED by `STALE_DOWNTIME_HOURS` (default 6, see
    `stale_downtime_hours`). STALLED is a state change for the row, not a timer:
    nothing is expired, G9 still holds, no case moves. It is the sentence
    "this has now gone on long enough that 'genuine outage' and 'we lost the
    webhook' are the only two things left, and one of them is on us", written
    where an operator who did not get woken by anything else will see it.
    """
    now = now or datetime.now()
    cfg = load_config()
    stale_h = stale_downtime_hours()
    since7 = (now - timedelta(days=7)).isoformat()

    unknown = [dict(r) for r in con.execute(
        "SELECT action_id, case_id, obligation_id, type, status, detail, execute_at,"
        " attempts FROM actions WHERE status = 'UNKNOWN' ORDER BY execute_at LIMIT 50")]
    failed = [dict(r) for r in con.execute(
        "SELECT action_id, case_id, obligation_id, type, status, detail, execute_at,"
        " attempts FROM actions WHERE status = 'FAILED' ORDER BY execute_at DESC LIMIT 50")]
    capped = [dict(r) for r in con.execute(
        "SELECT customer_id, COUNT(*) AS contacts_7d, MAX(sent_at) AS last_contact"
        " FROM contacts WHERE ok = 1 AND sent_at >= ? GROUP BY customer_id"
        " HAVING contacts_7d >= ? ORDER BY contacts_7d DESC LIMIT 50",
        (since7, cfg.max_contacts_7d))]
    top = [dict(r) for r in con.execute(
        "SELECT case_id, obligation_id, customer_id, amount, rung, attempts,"
        " failure_class, opened_at FROM cases WHERE run_id = 'live' AND status = 'OPEN'"
        " AND rung >= ? ORDER BY amount DESC LIMIT 50", (cfg.max_rung - 1,))]
    downtimes = [_downtime_row(r, now, stale_h) for r in store.open_downtimes(con)]
    stalled = [d for d in downtimes if d["stalled"]]

    return {
        "unknown_actions": unknown,
        "failed_actions": failed,
        "at_contact_cap": capped,
        "ladder_top": top,
        "open_downtimes": downtimes,
        "stalled_downtimes": stalled,
        "stale_downtime_hours": stale_h,
        "max_contacts_7d": cfg.max_contacts_7d,
        "max_rung": cfg.max_rung,
        "counts": {"unknown_actions": len(unknown), "failed_actions": len(failed),
                   "at_contact_cap": len(capped), "ladder_top": len(top),
                   "open_downtimes": len(downtimes),
                   "stalled_downtimes": len(stalled)},
    }


def _downtime_row(r: dict, now: datetime, stale_after_h: float) -> dict:
    """One unresolved outage, with how long it has been unresolved and what that means.

    `ends_at` is passed through untouched and is usually None. The row says
    "unknown" rather than filling in a number, because the whole point of item 3b
    is that we do not have one.

    `blocks` names the whole cost, not the obvious half: G9 blocks RETRY outright
    AND sets `wait_until`, and the engine defers a case for any gate that set one.
    So nothing goes out on this method while the row is open -- not a reminder, not
    a pay link.

    `state` is OPEN or STALLED, and STALLED is the one worth waking someone for.
    Nothing behind it changes: the row is not expired, G9 still holds, no case
    moves. What changes is that the row now says out loud what it has cost so far
    and what the two possible explanations are, because "open for 9h" on its own
    reads like weather rather than like a dropped webhook.

    An unreadable `began_at` yields `open_for_minutes = None` and state OPEN. That
    is the conservative direction: we do not know how long it has been open, so we
    do not claim it is stalled.
    """
    began = r.get("began_at") or r.get("seen_at")
    mins = None
    try:
        if began:
            mins = max(0, int((now - datetime.fromisoformat(began)).total_seconds() // 60))
    except (TypeError, ValueError):
        mins = None

    method = r.get("method")
    stalled = mins is not None and mins >= stale_after_h * 60
    # Same breakdown the Ops column shows. A note reading "held for 7h" beside a
    # column reading "6h 47m" is two numbers for one fact, and a reader has to stop
    # and work out whether they disagree.
    held = f"{mins // 60}h {mins % 60}m" if mins else "0m"
    return {**r,
            "open_for_minutes": mins,
            "state": "STALLED" if stalled else "OPEN",
            "stalled": stalled,
            "stale_after_hours": stale_after_h,
            "blocks": (f"RETRY on every {method} case, and defers the rest"
                       f" of the ladder on it"),
            "ends_at_known": bool(r.get("ends_at")),
            "note": (
                f"no .resolved received — recovery on {method} has been held for "
                f"{held}: RETRY is blocked and the rest of the ladder is deferred. "
                f"Either the outage is genuinely ongoing or we missed the resolve. "
                f"Clear it by replaying the resolved webhook, not by editing the table."
            ) if stalled else None}
