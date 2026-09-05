"""
The live feed: what the demo shows, and what it must never show.

Two of these tests exist because of bugs that were on screen. `test_an_action_
cancelled_before_its_time_is_not_a_future_event` is the one that matters most:
a cancelled action keeps the `execute_at` it was going to have, and filing it
by that timestamp put fourteen events that never happened at the top of a
newest-first feed. A feed that reports the future is worse than no feed.

The rest guard the collapse. It is the only place in this project where the UI
shows less than the database holds, so each thing it merges is pinned here: a
run of identical decisions on one case, one tick's identical verdict across many
cases, and a batch of identical action outcomes. Everything collapsed stays
individually stored and individually replayable -- `test_collapsing_hides_no_
rows` is what says so.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.repos import store
from app.services import feed as F

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


def at(seconds: int) -> str:
    """A timestamp `seconds` before NOW, so every row lands inside the window."""
    return (NOW - timedelta(seconds=seconds)).isoformat()


def decide(con, rows):
    """rows: (case_id, seconds_ago, action, stop_reason)."""
    store.save_decisions(con, [
        (f"dec_{cid}_{s}", "live", cid, at(s), action, reason,
         "{}", json.dumps([{"gate": "G12", "passed": False, "detail": "quiet"}]
                          if reason else []),
         "[]", "v1", "note", None)
        for cid, s, action, reason in rows])


def act(con, *, aid, case_id="c1", type_="REMIND", created=100, due=90,
        status="PENDING", detail=None):
    con.execute(
        "INSERT INTO actions (action_id, case_id, obligation_id, type, execute_at,"
        " status, idem_key, attempts, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, case_id, "ob_1", type_, at(due), status, aid, 0, detail, at(created)))
    con.commit()


def kinds(out, kind):
    return [e for e in out["events"] if e["kind"] == kind]


# -- the timestamp bug -----------------------------------------------------

def test_an_action_cancelled_before_its_time_is_not_a_future_event(con):
    """The bug this was written for: a pause cancels fourteen queued reminders,
    each still carrying the `execute_at` it was never going to reach, and a
    newest-first feed puts all fourteen above everything that actually happened.

    An action that never came due is not an execution. It is not lost either --
    the operator row that cancelled it carries the count."""
    act(con, aid="a_future", due=-3600, status="CANCELLED",   # due an hour from NOW
        detail="CANCELLED_BY_PAUSE (global) by asha")
    act(con, aid="a_past", due=60, status="DONE")

    out = F.feed(con, now=NOW)
    assert all(e["at"] <= NOW.isoformat() for e in out["events"]), \
        "the feed reported an event that has not happened yet"
    titles = [e["title"] for e in kinds(out, "execute")]
    assert titles == ["REMIND — done"]


def test_an_action_that_came_due_and_was_cancelled_still_shows(con):
    """The filter is on time, not on status. A pause that catches an action at
    the moment it runs is a real thing that happened to it."""
    act(con, aid="a1", due=30, status="CANCELLED",
        detail="CANCELLED_BY_PAUSE (global) at execution time")
    out = F.feed(con, now=NOW)
    assert [e["title"] for e in kinds(out, "execute")] == ["REMIND — cancelled"]


# -- collapsing ------------------------------------------------------------

def test_a_repeated_decision_is_one_line_with_a_count(con):
    """A case waiting on quiet hours is re-decided every tick. Sixty identical
    rows are one wait."""
    decide(con, [("c1", s, "WAIT", "QUIET_HOURS") for s in range(60, 0, -1)])
    d = kinds(F.feed(con, now=NOW), "decision")
    assert len(d) == 1
    assert d[0]["repeats"] == 60
    assert d[0]["at"] == at(60)             # dated from when the wait began
    assert d[0]["held_until"] == at(1)      # and says how long it held


def test_a_case_that_waits_acts_and_waits_again_shows_both_waits(con):
    """The reason this is a run and not a GROUP BY. Merging the two waits would
    report one wait, at the wrong time, that never happened."""
    decide(con, [("c1", 50, "WAIT", "QUIET_HOURS"),
                 ("c1", 40, "WAIT", "QUIET_HOURS"),
                 ("c1", 30, "REMIND", None),
                 ("c1", 20, "WAIT", "CONTACT_CAP"),
                 ("c1", 10, "WAIT", "CONTACT_CAP")])
    d = kinds(F.feed(con, now=NOW), "decision")
    assert [(e["at"], e["held_until"], e["detail"], e["repeats"]) for e in d] == [
        (at(20), at(10), "CONTACT_CAP", 2),
        (at(30), at(30), "note", 1),
        (at(50), at(40), "QUIET_HOURS", 2),
    ]


def test_one_verdict_on_many_cases_is_one_line(con):
    """Fourteen cases entering the same wait on the same tick is one judgement
    about fourteen cases, not fourteen judgements."""
    decide(con, [(f"c{i}", s, "WAIT", "QUIET_HOURS")
                 for i in range(14) for s in (50, 49, 48)])
    d = kinds(F.feed(con, now=NOW), "decision")
    assert len(d) == 1
    assert d[0]["cases"] == 14
    assert d[0]["repeats"] == 3             # each of them held for three ticks
    assert d[0]["case_id"] is None          # no single case owns this line
    assert len(d[0]["case_ids"]) == 3       # a few named, the rest counted


def test_cases_that_enter_a_state_at_different_times_stay_apart(con):
    """The fold is bounded by the second. Two cases starting to wait a minute
    apart are two things happening, and merging them would date one wrongly."""
    decide(con, [("c1", 120, "WAIT", "QUIET_HOURS"),
                 ("c2", 30, "WAIT", "QUIET_HOURS")])
    d = kinds(F.feed(con, now=NOW), "decision")
    assert len(d) == 2
    assert [e["cases"] for e in d] == [1, 1]


def test_identical_action_outcomes_in_one_second_are_one_line(con):
    for i in range(14):
        act(con, aid=f"a{i}", case_id=f"c{i}", due=30, status="CANCELLED",
            detail="CANCELLED_BY_PAUSE (global) by asha")
    e = kinds(F.feed(con, now=NOW), "execute")
    assert len(e) == 1
    assert e[0]["cases"] == 14
    # The count is a field, not a suffix on the title: one place in the UI
    # decides how "14 of these" is written, for every kind of row that can
    # stand for more than one thing.
    assert e[0]["title"] == "REMIND — cancelled"
    assert e[0]["case_id"] is None


def test_different_outcomes_in_one_second_are_not_merged(con):
    act(con, aid="a1", due=30, status="DONE")
    act(con, aid="a2", due=30, status="FAILED")
    act(con, aid="a3", due=30, status="UNKNOWN")
    assert len(kinds(F.feed(con, now=NOW), "execute")) == 3


def test_collapsing_hides_no_rows(con):
    """The promise the collapse rests on: the feed shows fewer lines, the
    database still holds every one. If this ever fails, the audit trail has been
    traded for a tidy screen."""
    decide(con, [("c1", s, "WAIT", "QUIET_HOURS") for s in range(40, 0, -1)])
    d = kinds(F.feed(con, now=NOW), "decision")
    stored = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert len(d) == 1 and d[0]["repeats"] == 40 and stored == 40
    # And the line points at a real row, so "replay" from the feed lands.
    assert con.execute("SELECT 1 FROM decisions WHERE decision_id = ?",
                       (d[0]["decision_id"],)).fetchone()


# -- the window and the page ----------------------------------------------

def test_the_window_is_a_bound_not_a_page(con):
    """Older than the window is gone, not paged. This is a feed; the archive is
    the Cases tab."""
    decide(con, [("c1", 30, "REMIND", None), ("c2", 60 * 60 * 3, "REMIND", None)])
    assert len(kinds(F.feed(con, minutes=30, now=NOW), "decision")) == 1
    assert len(kinds(F.feed(con, minutes=60 * 6, now=NOW), "decision")) == 2


def test_newest_first(con):
    decide(con, [("c1", 90, "REMIND", None), ("c2", 30, "RETRY", None)])
    act(con, aid="a1", created=60, due=59, status="DONE")
    ats = [e["at"] for e in F.feed(con, now=NOW)["events"]]
    assert ats == sorted(ats, reverse=True)


def test_the_page_is_bounded(con):
    decide(con, [(f"c{i}", 100 - i, "REMIND", None) for i in range(90)])
    out = F.feed(con, limit=10, now=NOW)
    assert len(out["events"]) == 10
    assert out["shown"] == 10


def test_an_empty_feed_is_empty_not_an_error(con):
    out = F.feed(con, now=NOW)
    assert out["events"] == [] and out["shown"] == 0
    assert out["window_minutes"] == F.DEFAULT_MINUTES


def test_a_stopped_decision_carries_the_gates_that_stopped_it(con):
    decide(con, [("c1", 20, "WAIT", "QUIET_HOURS")])
    d = kinds(F.feed(con, now=NOW), "decision")[0]
    assert d["gates_failed"] == ["G12: quiet"]


def test_the_benchmark_is_not_in_the_live_feed(con):
    """`run_id` is the only thing separating a live case from 2000 simulated
    ones. If this leaks, the demo shows a benchmark replaying itself."""
    store.save_decisions(con, [
        ("dec_bench", "default", "case_bench", at(10), "REMIND", None,
         "{}", "[]", "[]", "v1", "note", None)])
    assert kinds(F.feed(con, now=NOW), "decision") == []


# -- the walk --------------------------------------------------------------

def test_the_walk_stops_early_without_dropping_a_newer_line(con):
    """The backwards walk stops once it has closed enough runs, because reading
    the rest of the window cannot change the top of the page. What it must never
    do is stop in a way that drops something newer than what it kept."""
    # Alternating decisions on one case: one run per row, oldest to newest.
    decide(con, [("c1", s, "REMIND" if s % 2 else "RETRY", None)
                 for s in range(400, 0, -1)])
    out = F.feed(con, limit=5, now=NOW)
    ats = [e["at"] for e in kinds(out, "decision")]
    assert ats == [at(s) for s in (1, 2, 3, 4, 5)]
