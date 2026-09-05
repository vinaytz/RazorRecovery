"""
The live worker, and the one thing TIME_SCALE is not allowed to touch.

The worker is the live counterpart of the benchmark's tick loop: decide, schedule,
execute, on real cases in real time. What is pinned here:

  it is the same engine       decisions land in the same table, replayable
  TIME_SCALE compresses waits and ONLY waits -- `now` stays real
  TIME_SCALE cannot reach the benchmark, at any value
  a case that settled mid-flight is never contacted
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.controllers import execute as ex
from app.domain.models import ActionType, FailureClass, ObligationKind
from app.repos import store
from app.services.executor import ExecResult, ExecutorTimeout
from app.workers import live

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


class FakeExecutor:
    """Records what it was asked to do. Nothing leaves the process."""

    def __init__(self, settled=False, fail=False, timeout=False, contact=True):
        self.settled, self.fail, self.timeout, self.contact = settled, fail, timeout, contact
        self.calls: list[tuple] = []

    def fetch_obligation(self, oid):
        return {"settled": self.settled, "amount_due": 499_900}

    def execute(self, action_type, obligation_id, idem_key, case_id=None):
        self.calls.append((action_type, obligation_id, idem_key))
        if self.timeout:
            raise ExecutorTimeout("gateway did not answer in 8s")
        return ExecResult(ok=not self.fail, detail=f"{action_type} done",
                          contact_sent=self.contact and not self.fail)


def case(c, *, oid="order_L1", amount=499_900, fc=FailureClass.INSUFFICIENT_FUNDS,
         kind=ObligationKind.ORDER, rung=0, attempts=1, status="OPEN",
         customer="cust_L1", opened=None, ob_status="OPEN"):
    opened = opened or NOW - timedelta(hours=6)
    c.execute("INSERT OR REPLACE INTO obligations (id, customer_id, amount_due,"
              " amount_settled, status, opened_at, contact, email, name)"
              " VALUES (?,?,?,?,?,?,?,?,?)",
              (oid, customer, amount, 0, ob_status, opened.isoformat(),
               "+919000000001", "a@example.com", "Asha"))
    cid = f"live_{oid}"
    c.execute("INSERT OR REPLACE INTO cases (case_id, run_id, obligation_id, customer_id,"
              " arm, amount, failure_class, method, kind, rung, attempts, status,"
              " contacts_sent, actions_taken, opened_at, closed_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (cid, "live", oid, customer, "ENGINE", amount, fc.value, "card",
               kind.value, rung, attempts, status, 0, 0, opened.isoformat(), None))
    c.commit()
    return cid


def worker(c, executor=None):
    return live.LiveWorker(c, executor or FakeExecutor())


# -- TIME_SCALE: the delay, not the clock ---------------------------------

def test_time_scale_defaults_to_real_time(monkeypatch):
    monkeypatch.delenv("TIME_SCALE", raising=False)
    assert live.time_scale() == 1.0


@pytest.mark.parametrize("raw", ["", "0", "-1", "fast", "1e", "None"])
def test_a_nonsense_time_scale_falls_back_to_real_time(monkeypatch, raw):
    """A zero scale would divide by zero; a negative one would send every scheduled
    action into the past. Bad config must slow us down, never speed us up."""
    monkeypatch.setenv("TIME_SCALE", raw)
    assert live.time_scale() == 1.0


def test_scaled_compresses_a_six_hour_wait_into_six_seconds():
    at = NOW + timedelta(hours=6)
    assert live.scaled(at, NOW, 3600) == NOW + timedelta(seconds=6)


def test_scaled_leaves_real_time_alone():
    at = NOW + timedelta(hours=6)
    assert live.scaled(at, NOW, 1) == at


def test_an_overdue_action_stays_overdue():
    """max(0, ...) is the guard. Dividing a negative delay would push an action that
    is already due into the future -- a demo that quietly stops doing anything."""
    assert live.scaled(NOW - timedelta(hours=2), NOW, 3600) == NOW


def test_the_clock_itself_is_never_scaled(con, monkeypatch):
    """The reason TIME_SCALE is honest: timestamps stay real. `contacts_last_7d`
    still means seven real days and `opened_at` is when the case really opened."""
    monkeypatch.setenv("TIME_SCALE", "3600")
    cid = case(con)
    out = worker(con).tick(NOW)
    assert out["at"] == NOW.isoformat()
    row = con.execute("SELECT decided_at FROM decisions WHERE case_id=?", (cid,)).fetchone()
    assert row["decided_at"] == NOW.isoformat()


# -- the benchmark cannot feel any of this --------------------------------

# Invariant 3's tripwire. ONE constant, edited only when an item is explicitly
# allowed to change the decision core -- item 3a (G8 gained the no-mandate check),
# item 3c (the bandit gained time decay), item 3z (each arm got its own copy of
# the world). If this moves for any other reason, live-path code has leaked into
# `app/domain/` and the headline number is fiction.
#
# It is a VERY sensitive instrument, and item 3c measured how sensitive: a decay
# of 0.999999 -- which discards 0.07% of the evidence, a rounding error -- moves
# single-seed engine recovery by 4 points. Thompson sampling argmaxes over
# candidates whose probabilities differ in the 7th decimal, one flip changes an
# action, and the run diverges from there. So a moved hash means "the arithmetic
# path changed", NOT "the engine got better or worse". Only the seed sweep can
# say which, and see README "Bugs found in our own measurements".
BENCHMARK_STDOUT_MD5 = "763fcd5cb36db1593189c08f2c59c70e"


def _run_benchmark(**env_overrides) -> str:
    import os

    env = dict(os.environ, PYTHONPATH=".", WORKER="off", **env_overrides)
    r = subprocess.run([sys.executable, "run_benchmark.py", "--n", "2000"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


def test_time_scale_cannot_reach_the_benchmark():
    """Invariant 3, checked rather than asserted.

    A differential, not a hardcoded hash. The claim is "these env vars cannot reach
    the decision core", and the way to check that is to run the benchmark with them
    set to absurd values and with them absent, and require the two outputs to be
    byte-identical. Written this way it keeps testing the claim through every
    legitimate change to the core, instead of having to be re-pinned each time and
    briefly testing nothing while it is stale.

    Every live-path env knob belongs in this list, and a new one is a new way for
    the claim to become false quietly. `STALE_DOWNTIME_HOURS` (item 3b) joined it
    the day it was added.
    """
    absurd = _run_benchmark(TIME_SCALE="3600", ABANDON_MINUTES="1",
                            STALE_DOWNTIME_HOURS="0")
    plain = _run_benchmark(TIME_SCALE="1", ABANDON_MINUTES="30",
                           STALE_DOWNTIME_HOURS="6")
    assert absurd == plain, "a live-path env var changed the benchmark output"


def test_benchmark_stdout_md5_is_pinned():
    """The deliberate tripwire: the decision core produces exactly this output.

    Separate from the test above because it fails for a different reason. That one
    failing means an env var leaked into the core. This one failing means the core
    itself changed -- which is sometimes correct and always worth a human looking.
    """
    import hashlib

    got = hashlib.md5(_run_benchmark().encode()).hexdigest()
    assert got == BENCHMARK_STDOUT_MD5, (
        f"benchmark stdout md5 moved: {got} != {BENCHMARK_STDOUT_MD5}. If you did "
        "not deliberately change app/domain/, STOP -- something reached the core.")


def test_the_simulator_does_not_import_the_live_worker():
    """The other direction of the same wall: sim/ must not reach into the live path."""
    src = "\n".join(p.read_text() for p in sorted((ROOT / "sim").glob("*.py")))
    for leak in ("workers.live", "workers import live", "TIME_SCALE", "time_scale",
                 "LiveWorker", "sweeper"):
        assert leak not in src, f"'{leak}' leaked into sim/"


# -- one pass over a case -------------------------------------------------

def test_a_tick_decides_and_schedules(con):
    cid = case(con)
    out = worker(con).tick(NOW)
    assert len(out["decided"]) == 1
    d = out["decided"][0]
    assert d["case_id"] == cid
    row = con.execute("SELECT * FROM actions WHERE case_id=?", (cid,)).fetchone()
    if d["action"] in ("WAIT", "NONE"):
        assert row is None          # a legitimate answer, and nothing was scheduled
    else:
        assert row["type"] == d["action"] and row["status"] == "PENDING"


def test_the_decision_is_stored_and_replayable(con):
    """Same table and same shape as a benchmark decision, so the Decision tab can
    show a live case next to a simulated one."""
    cid = case(con)
    worker(con).tick(NOW)
    row = con.execute("SELECT * FROM decisions WHERE case_id=?", (cid,)).fetchone()
    assert row["run_id"] == "live" and row["config_version"]
    snap = store.get_decision(con, row["decision_id"])
    assert snap["snapshot"]["case_id"] == cid
    assert snap["gate_trace"][0]["gate"].startswith("G0")   # holdout first, live too


def test_a_second_tick_does_not_stack_a_second_action(con):
    """A pending action means we have already acted. Deciding again on top of it is
    how one failure becomes three emails.

    Ticks until something is scheduled rather than assuming the first one schedules:
    WAIT is a legitimate first answer and the point being pinned is what happens
    AFTER an action exists.
    """
    cid = case(con)
    w = worker(con)
    n = lambda: con.execute("SELECT COUNT(*) FROM actions WHERE case_id=?",   # noqa: E731
                            (cid,)).fetchone()[0]
    for i in range(20):
        w.tick(NOW + timedelta(seconds=i))          # far short of any scheduled delay
        if n():
            break
    assert n() == 1, "20 ticks and the worker never acted at all"
    for i in range(20, 30):
        w.tick(NOW + timedelta(seconds=i))
    assert n() == 1


def test_a_settled_case_is_not_decided_on(con):
    case(con, status="RECOVERED")
    assert worker(con).tick(NOW)["decided"] == []


def test_the_worker_survives_a_case_with_no_obligation(con):
    """A missing row is a data problem, not an outage. It must not stop the tick."""
    con.execute("INSERT INTO cases (case_id, run_id, obligation_id, customer_id, arm,"
                " amount, failure_class, method, kind, rung, attempts, status,"
                " contacts_sent, actions_taken, opened_at, closed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("live_orphan", "live", "order_GONE", "c", "ENGINE", 100_000,
                 "UNKNOWN", "card", "ORDER", 0, 1, "OPEN", 0, 0, NOW.isoformat(), None))
    con.commit()
    assert worker(con).tick(NOW)["decided"] == []          # skipped, not crashed


# -- execution ------------------------------------------------------------

def test_a_due_action_is_executed(con):
    cid = case(con)
    ex.schedule(con, cid, "order_L1", ActionType.PAY_LINK, NOW - timedelta(minutes=1))
    fake = FakeExecutor()
    out = worker(con, fake).tick(NOW)
    assert len(out["executed"]) == 1 and out["executed"][0]["status"] == "DONE"
    assert fake.calls[0][0] == "PAY_LINK"
    assert con.execute("SELECT contacts_sent FROM cases WHERE case_id=?",
                       (cid,)).fetchone()[0] == 1


def test_an_action_not_yet_due_is_left_alone(con):
    cid = case(con)
    ex.schedule(con, cid, "order_L1", ActionType.PAY_LINK, NOW + timedelta(hours=1))
    fake = FakeExecutor()
    assert worker(con, fake).tick(NOW)["executed"] == []
    assert fake.calls == []


def test_a_customer_who_paid_mid_flight_is_never_contacted(con):
    """The re-check in the executor, exercised through the worker. This is the
    failure the whole re-check exists to prevent."""
    cid = case(con)
    ex.schedule(con, cid, "order_L1", ActionType.PAY_LINK, NOW - timedelta(minutes=1))
    fake = FakeExecutor(settled=True)
    out = worker(con, fake).tick(NOW)
    assert out["executed"][0]["status"] == "ABORTED"
    assert out["executed"][0]["reason"] == "ALREADY_SETTLED"
    assert fake.calls == []                                # nothing was sent
    assert con.execute("SELECT contacts_sent FROM cases WHERE case_id=?",
                       (cid,)).fetchone()[0] == 0


def test_a_failed_send_does_not_count_as_a_contact(con):
    cid = case(con)
    ex.schedule(con, cid, "order_L1", ActionType.PAY_LINK, NOW - timedelta(minutes=1))
    out = worker(con, FakeExecutor(fail=True)).tick(NOW)
    assert out["executed"][0]["status"] == "FAILED"
    assert con.execute("SELECT contacts_sent FROM cases WHERE case_id=?",
                       (cid,)).fetchone()[0] == 0


def test_a_timeout_is_unknown_not_failed(con):
    cid = case(con)
    ex.schedule(con, cid, "order_L1", ActionType.RETRY, NOW - timedelta(minutes=1))
    out = worker(con, FakeExecutor(timeout=True)).tick(NOW)
    assert out["executed"][0]["status"] == "UNKNOWN"
    assert con.execute("SELECT status FROM actions WHERE case_id=?",
                       (cid,)).fetchone()[0] == "UNKNOWN"


def test_a_case_with_an_unknown_action_is_not_decided_on_again(con):
    """UNKNOWN means the money may have moved. Deciding again before the reconciler
    resolves it is how a customer gets charged twice."""
    cid = case(con)
    ex.schedule(con, cid, "order_L1", ActionType.RETRY, NOW - timedelta(minutes=1))
    w = worker(con, FakeExecutor(timeout=True))
    w.tick(NOW)
    assert w.tick(NOW + timedelta(seconds=1))["decided"] == []


# -- the snapshot is sourced, not invented --------------------------------

def test_the_snapshot_reads_the_contact_ledger(con):
    cid = case(con)
    store.record_contact(con, customer_id="cust_L1", obligation_id="order_L1",
                         case_id=cid, channel="email", action="REMIND", tier="static",
                         used_llm=False, subject="s", sent_at=(NOW - timedelta(days=1)).isoformat(),
                         ok=True, detail="sent")
    row = con.execute("SELECT * FROM cases WHERE case_id=?", (cid,)).fetchone()
    snap = worker(con).snapshot(row, NOW)
    assert snap.contacts_last_7d == 1
    assert snap.last_notice_sent_at is not None


def test_only_a_subscription_carries_a_mandate(con):
    """An order does not hold an instrument we can charge again. Claiming otherwise
    is what makes RETRY look free -- see item 3a."""
    w = worker(con)
    case(con, oid="order_ONE", kind=ObligationKind.ORDER)
    case(con, oid="order_SUB", kind=ObligationKind.SUBSCRIPTION)
    rows = {r["case_id"]: r for r in con.execute("SELECT * FROM cases")}
    assert w.snapshot(rows["live_order_ONE"], NOW).is_mandate is False
    assert w.snapshot(rows["live_order_SUB"], NOW).is_mandate is True


def test_the_snapshot_knows_a_settled_obligation(con):
    case(con, oid="order_S", ob_status="SETTLED")
    row = con.execute("SELECT * FROM cases WHERE case_id='live_order_S'").fetchone()
    assert worker(con).snapshot(row, NOW).obligation_settled is True


def test_unpopulated_fields_are_conservative_not_invented(con):
    """`promised_until`, `opted_out` and `method_in_downtime` have no live source
    yet (items 3b and 3e). They must read as "no reason to hold back", not as a
    plausible guess -- a guessed promise would silence a real debt."""
    case(con, oid="order_C")
    row = con.execute("SELECT * FROM cases WHERE case_id='live_order_C'").fetchone()
    snap = worker(con).snapshot(row, NOW)
    assert snap.promised_until is None
    assert snap.opted_out is False
    assert snap.method_in_downtime is False and snap.downtime_ends_at is None


def test_the_rung_only_climbs(con):
    w = worker(con)
    assert w.rung_of(ActionType.REMIND, 3) == 3        # would descend -- refused
    assert w.rung_of(ActionType.PAY_LINK, 1) == 3
    assert w.rung_of(ActionType.NONE, 2) == 2         # not on the ladder at all


# -- the worker is seeded -------------------------------------------------

def test_two_workers_with_the_same_seed_decide_the_same_way(con):
    """Invariant 7 on the live path. Thompson sampling draws from an rng, and an
    unseeded worker would make the same case decide differently on a re-run."""
    import numpy as np

    case(con, oid="order_R1")
    a = live.LiveWorker(con, FakeExecutor(), rng=np.random.default_rng(7)).tick(NOW)
    con.execute("DELETE FROM actions"); con.execute("DELETE FROM decisions"); con.commit()
    b = live.LiveWorker(con, FakeExecutor(), rng=np.random.default_rng(7)).tick(NOW)
    assert [d["action"] for d in a["decided"]] == [d["action"] for d in b["decided"]]
