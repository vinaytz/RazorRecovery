"""
The live event feed. Everything that happened, newest first, in one list.

This is the demo surface, and it is assembled from the tables that already hold
the truth rather than from a log. Nothing here is written by anybody: if a row
is not in `events`, `cases`, `decisions`, `actions`, `contacts`, `settlements`,
`pauses` or `ops_audit`, it does not appear, and if it is, it does.

WHY DECISIONS ARE COLLAPSED, AND WHAT IS NOT BEING HIDDEN.

The live worker re-decides every open case on every tick, because that is what
WAIT means here: ask again in a second. A case waiting on quiet hours therefore
produces one identical decision row per second, and by mid-morning the decisions
table holds tens of thousands of them. Every one is stored and every one is
replayable -- nothing is thrown away.

What a feed of them would show, though, is a wall of `WAIT / QUIET_HOURS`
scrolling past at 14 lines a second, which is not a view of the system; it is a
view of the tick loop. So the feed shows the TRANSITIONS: the first decision of
each run of identical ones, carrying the number of times it was repeated and the
time it last held. A run, not a `GROUP BY` -- a case that waits, acts, and waits
again shows two waits, at the two times it actually waited, rather than one
merged wait at the wrong time.

TIMESTAMPS ARE THE ONES IN THE DATABASE, INCLUDING THE AWKWARD ONE.

`actions` has no `executed_at` column: it has `created_at` (when the engine chose
it) and `execute_at` (when it came due). A finished action is filed here under
`execute_at`, which is within a tick of when it really ran because the worker
runs whatever is due on the tick it becomes due. That is a real approximation and
it is named here rather than smoothed over.

It also has a consequence worth stating, because it was a bug here before it was
a docstring. An action cancelled before it ever came due keeps the `execute_at`
it was going to have, which is in the FUTURE -- so filing it by `execute_at` put
events at the top of a newest-first feed that had not happened and now never
would. Fourteen of them, from one pause, above everything real. The execute
source therefore reports only what has actually come due (`execute_at <= now`).
Actions cancelled before that point are not execution events at all, and they
are not lost: the operator line that cancelled them carries the count, and a
cancellation caused by the money arriving is reported by the settlement.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

# One page of feed. Deliberately small: this is polled once a second, and a feed
# nobody can read the bottom of is a log file with extra steps.
DEFAULT_LIMIT = 60

# How far back each source is asked. Bounded because `decisions` grows by one row
# per open case per second, so an unbounded window would read the whole day's tick
# churn on every poll.
DEFAULT_MINUTES = 30


def feed(con, *, limit: int = DEFAULT_LIMIT, minutes: int = DEFAULT_MINUTES,
         now: datetime | None = None) -> dict:
    """The last `limit` things that happened, newest first."""
    now = now or datetime.now()
    since = (now - timedelta(minutes=max(1, int(minutes)))).isoformat()

    rows: list[dict] = []
    rows += _webhooks(con, since)
    rows += _cases(con, since)
    rows += _decisions(con, since, limit)
    rows += _actions(con, since, now)
    rows += _contacts(con, since)
    rows += _settlements(con, since)
    rows += _operator(con, since)

    rows.sort(key=lambda r: r["at"], reverse=True)
    out = rows[:limit]
    return {
        "at": now.isoformat(),
        "since": since,
        "window_minutes": minutes,
        "events": out,
        "shown": len(out),
        "candidates": len(rows),
        "note": ("assembled from the tables themselves, not from a log. a run of "
                 "identical decisions on one case, and one tick's identical "
                 "verdict across many cases, each collapse to a single line with "
                 "a count -- every decision behind them is still stored and still "
                 "replayable"),
    }


# -- sources ---------------------------------------------------------------

def _webhooks(con, since: str) -> list[dict]:
    return [{
        "at": r["received_at"], "kind": "webhook", "case_id": None,
        "obligation_id": r["obligation_id"],
        "title": r["type"] or "event",
        "detail": f"accepted and deduplicated on {r['dedupe_key']}",
        "tone": "",
    } for r in con.execute(
        "SELECT * FROM events WHERE received_at >= ? ORDER BY id DESC LIMIT 40",
        (since,))]


CLOSED_TONE = {"RECOVERED": "good", "SELF_RECOVERED": "good",
               "WRITTEN_OFF": "warn", "ESCALATED": "warn"}

CLOSED_WORDS = {
    "RECOVERED": "closed — the money arrived after we acted",
    "SELF_RECOVERED": "closed — they paid without us contacting them",
    "WRITTEN_OFF": "closed — the ladder ran out and nobody was contacted again",
    "ESCALATED": "handed to a human",
}


def _cases(con, since: str) -> list[dict]:
    out = []
    for r in con.execute(
            "SELECT * FROM cases WHERE run_id = 'live' AND opened_at >= ?"
            " ORDER BY opened_at DESC LIMIT 40", (since,)):
        out.append({
            "at": r["opened_at"], "kind": "case", "case_id": r["case_id"],
            "obligation_id": r["obligation_id"],
            "title": "Case opened",
            "detail": f"{r['failure_class']} on {r['method']}",
            "amount": r["amount"], "tone": "warn",
        })
    for r in con.execute(
            "SELECT * FROM cases WHERE run_id = 'live' AND closed_at >= ?"
            " ORDER BY closed_at DESC LIMIT 40", (since,)):
        out.append({
            "at": r["closed_at"], "kind": "case", "case_id": r["case_id"],
            "obligation_id": r["obligation_id"],
            "title": (r["status"] or "closed").replace("_", " ").capitalize(),
            "detail": CLOSED_WORDS.get(r["status"], "closed"),
            "amount": r["amount"], "tone": CLOSED_TONE.get(r["status"], ""),
        })
    return out


# The first decision of each run of identical ones, with how long the run lasted
# and how many times it repeated.
#
# This was a window-function query and is now a backwards walk, for one reason:
# it is polled every second on the connection the live worker writes through, and
# collapsing 12k rows in SQL took 68ms of every tick. Reading the same rows and
# folding them in Python costs 30ms, and usually far less, because walking newest
# -> oldest lets it stop early. Once `limit` runs have been CLOSED, every run
# still open or still unread began at or before the timestamp we have walked back
# to, so none of them can outrank what we already hold, and the rest of the
# window does not need to be read at all.
_DECISIONS_SQL = """
SELECT decision_id, case_id, decided_at, action, stop_reason, notes
FROM decisions WHERE run_id = 'live' AND decided_at >= ?
ORDER BY decided_at DESC
"""


# How many decision runs the backwards walk will close before it stops. A
# multiple of the page size rather than the page size itself, because the fold
# across cases happens after the walk: fourteen runs can become one line, and a
# walk that stopped at exactly `limit` runs would hand back a nearly empty page
# while the rest of the window sat unread.
RUN_BUDGET = 8


def _decisions(con, since: str, limit: int) -> list[dict]:
    budget = limit * RUN_BUDGET
    open_runs: dict[str, dict] = {}
    closed: list[dict] = []
    stopped_early = False

    for r in con.execute(_DECISIONS_SQL, (since,)):
        cid = r["case_id"]
        key = (r["action"], r["stop_reason"] or "")
        cur = open_runs.get(cid)
        if cur is not None and cur["key"] == key:
            # Same run, one tick earlier. The run's start moves back and the id
            # carried is the earliest seen, so "at" and "decision_id" agree:
            # the line is dated and replays from its first decision.
            cur["at"] = r["decided_at"]
            cur["decision_id"] = r["decision_id"]
            cur["notes"] = r["notes"]
            cur["repeats"] += 1
            continue
        if cur is not None:
            closed.append(cur)
            if len(closed) >= budget:
                stopped_early = True
                break
        open_runs[cid] = {
            "key": key, "case_id": cid, "at": r["decided_at"],
            "held_until": r["decided_at"], "repeats": 1,
            "decision_id": r["decision_id"], "notes": r["notes"],
        }

    # A run still open when the walk stopped began at or before the point we
    # stopped at, and every closed run began after it, so no open run can reach
    # the page. Dropping them is what makes stopping early safe -- and it also
    # keeps a half-counted `repeats` from ever being rendered.
    runs = closed if stopped_early else closed + list(open_runs.values())
    rows = _fold_across_cases(runs)[:limit]
    if not rows:
        return []

    # Failing gates, for the ones actually being shown. The trace is large and
    # there is no reason to read tens of thousands of them to render sixty lines.
    # Any row of a run replays identically -- that is what makes it a run -- so
    # the id carried here is one of them and the time shown is the run's first.
    ids = [d["decision_id"] for d in rows]
    q = ",".join("?" * len(ids))
    traces = {r["decision_id"]: _failed_gates(r["gate_trace"]) for r in con.execute(
        f"SELECT decision_id, gate_trace FROM decisions WHERE decision_id IN ({q})",
        ids)}

    out = []
    for d in rows:
        action, reason = d["key"]
        waiting = action in ("WAIT", "NONE")
        out.append({
            "at": d["at"], "kind": "decision",
            "case_id": d["case_id"] if d["cases"] == 1 else None,
            "case_ids": d["case_ids"],
            "cases": d["cases"],
            "decision_id": d["decision_id"],
            "title": "Waiting" if waiting else f"Decided: {action}",
            "detail": reason or d["notes"] or "chosen on expected uplift",
            "gates_failed": traces.get(d["decision_id"], []),
            "repeats": d["repeats"],
            "held_until": d["held_until"],
            "tone": "" if waiting else "act",
        })
    return out


def _fold_across_cases(runs: list[dict]) -> list[dict]:
    """One tick's verdict on many cases is one line, not one line per case.

    Collapsing a case's repeated decisions fixes churn down the time axis and
    leaves the other one untouched: the worker decides every open case on the
    same tick, so fourteen cases entering the quiet-hours wait together produced
    fourteen identical lines, all with the same timestamp, all saying the same
    thing. That is one judgement about fourteen cases.

    The key is the second, not the page, so cases that enter a state at
    different times stay separate -- those really are different events. The
    surviving line carries how many cases and a few of their ids, and its
    `decision_id` is one of them, so "replay this" still lands somewhere real.
    """
    groups: dict[tuple, dict] = {}
    for d in sorted(runs, key=lambda d: d["at"], reverse=True):
        k = (d["at"][:19], d["key"])
        g = groups.get(k)
        if g is None:
            groups[k] = dict(d, cases=1, case_ids=[d["case_id"]])
            continue
        g["cases"] += 1
        if len(g["case_ids"]) < 3:
            g["case_ids"].append(d["case_id"])
        # The line should read as the longest any of these has been held, and
        # be dated from the earliest of them, so it never claims to be newer or
        # shorter-lived than the thing it stands for.
        g["repeats"] = max(g["repeats"], d["repeats"])
        g["at"] = min(g["at"], d["at"])
        g["held_until"] = max(g["held_until"], d["held_until"])
    return sorted(groups.values(), key=lambda d: d["at"], reverse=True)


def _failed_gates(blob: str | None) -> list[str]:
    """Which gates said no. The trace is the engine's own reason to stop."""
    try:
        trace = json.loads(blob or "[]")
    except (TypeError, ValueError):
        return []
    return [f"{g.get('gate')}: {g.get('detail')}" for g in trace
            if isinstance(g, dict) and not g.get("passed")]


EXEC_TONE = {"DONE": "good", "FAILED": "bad", "UNKNOWN": "warn",
             "CANCELLED": "stop", "ABORTED": ""}

EXEC_WORDS = {
    "DONE": "executed",
    "FAILED": "the action ran and did not work — the rung was spent either way",
    "UNKNOWN": "timed out — we do not know whether money moved, so we say so",
    "CANCELLED": "cancelled before it ran",
    "ABORTED": "stopped on the pre-flight re-check — the debt was already settled",
}


def _actions(con, since: str, now: datetime) -> list[dict]:
    out = []
    # Grouped by the second, for the same reason the decisions are: one tick
    # that queues three reminders is one thing the engine did.
    for r in con.execute(
            "SELECT SUBSTR(created_at, 1, 19) AS at, type, COUNT(*) AS n,"
            "       MIN(execute_at) AS due, MIN(case_id) AS case_id,"
            "       MIN(obligation_id) AS obligation_id, MIN(action_id) AS action_id"
            " FROM actions WHERE created_at >= ?"
            " GROUP BY SUBSTR(created_at, 1, 19), type"
            " ORDER BY at DESC LIMIT 60", (since,)):
        n = int(r["n"])
        out.append({
            "at": r["at"], "kind": "schedule",
            "case_id": r["case_id"] if n == 1 else None,
            "obligation_id": r["obligation_id"] if n == 1 else None,
            "action_id": r["action_id"] if n == 1 else None,
            "cases": n,
            "title": f"Scheduled: {r['type']}",
            "detail": f"due {(r['due'] or '')[11:19]}, idempotency key held",
            "tone": "",
        })

    # Only what has actually come due. See the note at the top of the module: an
    # action cancelled before its time still carries the future `execute_at` it
    # was going to have, and filing that in a newest-first feed put events that
    # never happened above every event that did.
    #
    # Grouped the same way, because one operator decision can end many actions at
    # once and fourteen identical lines is a worse account of that than one line
    # saying fourteen.
    for r in con.execute(
            "SELECT SUBSTR(execute_at, 1, 19) AS at, type, status, detail,"
            "       COUNT(*) AS n, MIN(case_id) AS case_id,"
            "       MIN(obligation_id) AS obligation_id, MIN(action_id) AS action_id"
            " FROM actions"
            " WHERE execute_at >= ? AND execute_at <= ? AND status <> 'PENDING'"
            " GROUP BY SUBSTR(execute_at, 1, 19), type, status, IFNULL(detail, '')"
            " ORDER BY at DESC LIMIT 60", (since, now.isoformat())):
        n = int(r["n"])
        out.append({
            "at": r["at"], "kind": "execute",
            "case_id": r["case_id"] if n == 1 else None,
            "obligation_id": r["obligation_id"] if n == 1 else None,
            "action_id": r["action_id"] if n == 1 else None,
            "cases": n,
            "title": f"{r['type']} — {(r['status'] or '').lower()}",
            "detail": r["detail"] or EXEC_WORDS.get(r["status"], ""),
            "tone": EXEC_TONE.get(r["status"], ""),
        })
    return out


def _contacts(con, since: str) -> list[dict]:
    return [{
        "at": r["sent_at"], "kind": "contact", "case_id": r["case_id"],
        "obligation_id": r["obligation_id"],
        "title": ("Contacted the customer" if r["ok"] else "Contact failed"),
        "detail": (f"{r['channel']} · {r['subject']}"
                   f" · {'LLM' if r['used_llm'] else 'template'} copy"
                   + ("" if r["ok"] else f" · {r['detail']} · no cap was burnt")),
        "tone": "good" if r["ok"] else "bad",
    } for r in con.execute(
        "SELECT * FROM contacts WHERE sent_at >= ? ORDER BY id DESC LIMIT 40",
        (since,))]


def _settlements(con, since: str) -> list[dict]:
    return [{
        "at": r["settled_at"], "kind": "settle", "case_id": r["case_id"],
        "obligation_id": r["obligation_id"], "amount": r["amount"],
        "title": "Money arrived",
        "detail": (f"matched on {r['match_basis']} · level {r['match_level']}"
                   f" · {r['match_confidence']} · "
                   + ("attributed to us: " + str(r["attribution_reason"])
                      if r["attributed"] else "not attributed to us")),
        "tone": "good",
    } for r in con.execute(
        "SELECT * FROM settlements WHERE settled_at >= ? ORDER BY id DESC LIMIT 40",
        (since,))]


def _operator(con, since: str) -> list[dict]:
    """A human's decisions, in the same list as the engine's, clearly labelled."""
    out = []
    for r in con.execute(
            "SELECT * FROM pauses WHERE created_at >= ? OR lifted_at >= ?"
            " ORDER BY id DESC LIMIT 20", (since, since)):
        scope = "everything" if r["scope"] == "global" else f"{r['scope']} {r['value']}"
        if (r["created_at"] or "") >= since:
            out.append({
                "at": r["created_at"], "kind": "operator", "case_id": None,
                "title": f"{r['who']} paused {scope}",
                "detail": (f"{r['reason'] or 'no reason given'} · {r['cancelled']}"
                           " queued action(s) cancelled · ingestion continues"),
                "tone": "stop",
            })
        if (r["lifted_at"] or "") >= since:
            out.append({
                "at": r["lifted_at"], "kind": "operator", "case_id": None,
                "title": f"{r['lifted_by']} resumed {scope}",
                "detail": "cases pick up from the rung they had already climbed",
                "tone": "act",
            })
    for r in con.execute(
            "SELECT * FROM ops_audit WHERE at >= ? AND key NOT LIKE 'pause%'"
            " AND key <> 'resume' ORDER BY id DESC LIMIT 20", (since,)):
        out.append({
            "at": r["at"], "kind": "operator", "case_id": None,
            "title": f"{r['who']} changed {r['key']}",
            "detail": f"{_short(r['before'])} → {_short(r['after'])}"
                      + (f" · {r['note']}" if r["note"] else ""),
            "tone": "act",
        })
    return out


def _short(v: str | None) -> str:
    if v is None:
        return "unset"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "…"
