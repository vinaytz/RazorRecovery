"""Item 3b: the downtime feed, and the end time we refuse to invent.

Razorpay tells us an issuer is down. Gate G9 has blocked RETRY on that method
since P2 -- but nothing populated `method_in_downtime` on a live snapshot, so on
the live path G9 was a gate wired to a constant `False`. The ingest branch replied
"downtime recorded -- gate G9 blocks RETRY while it holds" and recorded nothing,
which is worse than not handling the event: a false verdict is a claim of coverage.

Four things are pinned here, and the last two are the ones that matter.

  1. `.started` blocks RETRY on that method; `.resolved` unblocks it.
  2. Redelivery is idempotent, in both directions. A `.started` arriving after its
     own `.resolved` -- webhook order is not guaranteed -- must not reopen it.
  3. NO END TIME IS EVER GUESSED. A live outage sends `end: null`, so
     `downtime_ends_at` stays None and G9 falls back to a re-check interval. Only
     a payload that actually carries an `end` populates it.
  4. NOTHING EXPIRES A ROW ON A TIMER. A dropped `.resolved` does not just block
     retries on that method -- G9 sets `wait_until`, so the engine defers every
     case on it (`test_a_downtime_defers_the_whole_case_not_only_the_retry`) until
     `window_hours` closes and writes it off. That is a total cost on that rail, and
     it would show up as write-offs a week later rather than as an outage. The
     deliberate alternative, ageing the row out after N hours, is exactly the guess
     (3) forbids: it would have this system decide an outage was over with no
     evidence, and send customers at a dead rail silently. So the stuck row is made
     loud on the Ops attention list instead, and a human is the escape hatch.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.controllers import ingest as ing
from app.domain.gates import run_gates
from app.domain.models import ActionType, FailureClass, ObligationKind, StopReason
from app.config_loader import load_config
from app.repos import store
from app.services import ops
from app.workers import live

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "webhooks"
CFG = load_config()

# After the fixtures' epochs (Feb 2026), so an outage they opened reads as ongoing.
NOW = datetime(2026, 3, 10, 14, 0, 0)


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def started(**over) -> dict:
    """The real `.started` payload, with the entity fields a test wants changed."""
    p = load("05_payment_downtime_started.json")
    p["payload"]["payment.downtime"]["entity"].update(over)
    return p


def resolved(**over) -> dict:
    p = load("09_payment_downtime_resolved.json")
    p["payload"]["payment.downtime"]["entity"].update(over)
    return p


def send(c, payload: dict, now: datetime = NOW) -> dict:
    return ing.ingest(c, payload, {"x-razorpay-event-id": payload["id"]}, now=now)


def case(c, *, oid="order_D1", method="netbanking", kind=ObligationKind.SUBSCRIPTION,
         amount=499_900, customer="cust_D1"):
    """A live case on `method`.

    A SUBSCRIPTION by default, because RETRY is the action under test and item 3a's
    G8 blocks it outright on anything holding no mandate. A test asserting "RETRY is
    blocked" on an ORDER would pass with the downtime feed ripped out entirely --
    that is the failure mode README's "bugs found in our own measurements" section
    describes, and it is not worth repeating.
    """
    opened = NOW - timedelta(hours=6)
    c.execute("INSERT OR REPLACE INTO obligations (id, customer_id, amount_due,"
              " amount_settled, status, opened_at, contact, email, name)"
              " VALUES (?,?,?,?,?,?,?,?,?)",
              (oid, customer, amount, 0, "OPEN", opened.isoformat(),
               "+919000000001", "a@example.com", "Asha"))
    cid = f"live_{oid}"
    c.execute("INSERT OR REPLACE INTO cases (case_id, run_id, obligation_id, customer_id,"
              " arm, amount, failure_class, method, kind, rung, attempts, status,"
              " contacts_sent, actions_taken, opened_at, closed_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (cid, "live", oid, customer, "ENGINE", amount,
               FailureClass.INSUFFICIENT_FUNDS.value, method, kind.value,
               0, 1, "OPEN", 0, 0, opened.isoformat(), None))
    # A mandate whose pre-debit notice was served 48h ago, so the only thing that
    # can block RETRY in these tests is G9.
    c.execute("INSERT INTO contacts (customer_id, obligation_id, case_id, channel,"
              " action, sent_at, ok, detail) VALUES (?,?,?,?,?,?,?,?)",
              (customer, oid, cid, "email", "pre_debit_notice",
               (NOW - timedelta(hours=48)).isoformat(), 1, "notice"))
    c.commit()
    return cid


def snapshot_of(c, case_id: str, now: datetime = NOW):
    row = c.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    return live.LiveWorker(c, executor=None).snapshot(row, now)


def worth_acting_on(c, **over):
    """A case the engine actively WANTS to act on, so a WAIT means something.

    The two tests below are about the engine choosing to do nothing. That only
    proves anything if the same case would otherwise choose to do something, and by
    default it would not: at Rs 4,999 with an untrained posterior every action is
    EV-negative and the case waits for reasons of its own.

    So: Rs 50,000, and RETRY *and* REMIND both trained to succeed against a NONE
    arm that never does. Both rungs matter -- RETRY is the one the ladder offers
    first, and REMIND is the one it falls to when G9 blocks RETRY. Training only
    REMIND (the first draft) left RETRY as the sole candidate and the case waited on
    EV, which looks exactly like the assertion passing.

    Returns (case_id, posterior, case row, worker).
    """
    import numpy as np
    from app.services.bandit import Posterior

    cid = case(c, method="netbanking", amount=5_000_000, **over)
    row = c.execute("SELECT * FROM cases WHERE case_id = ?", (cid,)).fetchone()
    w = live.LiveWorker(c, executor=None, rng=np.random.default_rng(7))
    post = Posterior(CFG)
    seg = w.snapshot(row, NOW).segment
    for _ in range(80):
        post.update(seg, ActionType.RETRY, True)
        post.update(seg, ActionType.REMIND, True)
        post.update(seg, ActionType.NONE, False)
    return cid, post, row, w


# -- the round trip --------------------------------------------------------

def test_started_reaches_the_snapshot_and_blocks_retry(con):
    """The whole point of the item, end to end: webhook -> db -> snapshot -> gate."""
    cid = case(con, method="netbanking")

    before = snapshot_of(con, cid)
    assert before.method_in_downtime is False
    assert ActionType.RETRY not in run_gates(before, CFG).blocked_actions, (
        "RETRY must be legal before the outage, or this test proves nothing")

    out = send(con, started())
    assert out["action"] == "downtime_recorded"
    assert out["method"] == "netbanking"

    after = snapshot_of(con, cid)
    assert after.method_in_downtime is True
    g = run_gates(after, CFG)
    assert ActionType.RETRY in g.blocked_actions
    assert not g.blocked, ("an outage defers the case, it does not terminate it -- "
                          "a hard stop here would close a recoverable debt")
    assert g.wait_until is not None and g.wait_reason == StopReason.DOWNTIME


def test_resolved_unblocks_it(con):
    cid = case(con, method="netbanking")
    send(con, started())
    assert snapshot_of(con, cid).method_in_downtime is True

    out = send(con, resolved())
    assert out["action"] == "downtime_resolved"

    snap = snapshot_of(con, cid)
    assert snap.method_in_downtime is False
    assert snap.downtime_ends_at is None
    assert ActionType.RETRY not in run_gates(snap, CFG).blocked_actions


def test_a_downtime_on_another_method_does_not_block_this_one(con):
    """Over-blocking by method is admitted; over-blocking across methods is a bug."""
    cid = case(con, method="card")
    send(con, started())                       # netbanking
    snap = snapshot_of(con, cid)
    assert snap.method_in_downtime is False
    assert ActionType.RETRY not in run_gates(snap, CFG).blocked_actions


# -- the end time we do not have -------------------------------------------

def test_a_live_outage_stores_no_end_time(con):
    """`end: null` stays null. G9's backoff is a re-check, not a forecast."""
    send(con, started())
    row = store.active_downtime(con, "netbanking")
    assert row is not None
    assert row["ends_at"] is None
    assert row["began_at"] is not None          # the START is a fact we were given

    cid = case(con, method="netbanking")
    snap = snapshot_of(con, cid)
    assert snap.method_in_downtime is True
    assert snap.downtime_ends_at is None

    # G9 still names a time to look again, and it is the config backoff -- derived
    # from `now`, not from the outage. The distinction is the item.
    g = run_gates(snap, CFG)
    assert g.wait_until == NOW + timedelta(minutes=CFG.downtime_backoff_minutes)


def test_a_scheduled_window_that_carries_an_end_does_populate_it(con):
    """The one case with a real end time: Razorpay told us. Copied, not computed."""
    end = int((NOW + timedelta(hours=2)).timestamp())
    send(con, started(id="down_TEST_SCHED", scheduled=True, end=end,
                      method="netbanking"))
    row = store.active_downtime(con, "netbanking")
    assert row["ends_at"] == datetime.fromtimestamp(end).isoformat()
    assert row["scheduled"] == 1

    cid = case(con, method="netbanking")
    snap = snapshot_of(con, cid)
    assert snap.downtime_ends_at == datetime.fromtimestamp(end)
    # And G9 waits until Razorpay's end, not until its own backoff.
    assert run_gates(snap, CFG).wait_until == datetime.fromtimestamp(end)


def test_an_unreadable_stored_end_time_degrades_to_unknown(con):
    """A corrupt timestamp must not become a timestamp we trust."""
    send(con, started())
    con.execute("UPDATE downtimes SET ends_at = 'not a date'")
    con.commit()
    cid = case(con, method="netbanking")
    snap = snapshot_of(con, cid)
    assert snap.method_in_downtime is True       # still blocked
    assert snap.downtime_ends_at is None         # but with no end we pretend to know


# -- redelivery, in both directions ----------------------------------------

def test_a_redelivered_start_is_one_outage(con):
    first = send(con, started())
    # Same downtime, new event id, so the events table does not absorb it and the
    # downtime handler is the thing being tested.
    again = dict(started(), id="evt_TEST0000000005_retry")
    second = send(con, again)

    assert first["action"] == "downtime_recorded"
    assert second["action"] == "downtime_already_known"
    assert con.execute("SELECT COUNT(*) FROM downtimes").fetchone()[0] == 1


def test_a_start_redelivered_after_its_resolve_does_not_reopen_it(con):
    """Webhook order is not guaranteed. This is the one that would hurt.

    A late `.started` re-blocking a method whose outage is long over would take
    RETRY away with no event left in flight to give it back.
    """
    cid = case(con, method="netbanking")
    send(con, started())
    send(con, resolved())
    send(con, dict(started(), id="evt_TEST0000000005_late"))

    row = con.execute("SELECT status FROM downtimes").fetchone()
    assert row["status"] == "resolved"
    assert snapshot_of(con, cid).method_in_downtime is False


def test_a_resolve_for_an_outage_we_never_saw_still_clears_the_method(con):
    """The restart case, and why the fallback is by method.

    An outage that started before this process did leaves no row keyed on its id.
    Refusing the resolve would block RETRY on that method with nothing left that
    could lift it. Clearing by method is broader, and broader in the direction of
    letting money move -- which is only safe because the execute path re-checks
    payment state before every action regardless.
    """
    send(con, started())
    out = send(con, resolved(id="down_SOMETHING_ELSE"))
    assert out["action"] == "downtime_resolved"
    assert store.active_downtime(con, "netbanking") is None


def test_a_resolve_with_nothing_open_changes_nothing_and_says_so(con):
    out = send(con, resolved())
    assert out["action"] == "downtime_resolve_ignored"
    assert "nothing changed" in out["verdict"]
    assert con.execute("SELECT COUNT(*) FROM downtimes").fetchone()[0] == 0


def test_the_verdict_no_longer_claims_something_that_did_not_happen(con):
    """The branch used to reply with this sentence while recording nothing."""
    out = send(con, started())
    assert "G9 blocks RETRY" in out["verdict"]
    assert store.active_downtime(con, "netbanking") is not None, (
        "the verdict says the gate will block -- the row that makes that true "
        "must exist")


# -- the stuck row ---------------------------------------------------------

def test_an_open_downtime_is_on_the_attention_list_with_no_end_time_claimed(con):
    send(con, started())
    a = ops.attention(con, NOW)

    assert a["counts"]["open_downtimes"] == 1
    row = a["open_downtimes"][0]
    assert row["method"] == "netbanking"
    assert row["ends_at_known"] is False
    assert row["open_for_minutes"] > 0
    assert "RETRY" in row["blocks"]
    assert json.loads(row["instrument"]) == {"bank": "HDFC"}

    send(con, resolved())
    assert ops.attention(con, NOW)["counts"]["open_downtimes"] == 0


def test_nothing_ages_a_downtime_out_on_its_own(con):
    """Two weeks later, still blocked, still visible. Both halves are the design.

    If a future change adds an expiry here, this test fails -- and the thing to do
    is not to delete it. Expiring the row means deciding when the outage ended,
    which is the guess the whole item refuses to make. The escape hatch for a
    genuinely stuck row is a human reading the attention list.
    """
    cid = case(con, method="netbanking")
    send(con, started())
    later = NOW + timedelta(days=14)

    assert snapshot_of(con, cid, later).method_in_downtime is True
    row = ops.attention(con, later)["open_downtimes"][0]
    assert row["open_for_minutes"] > 14 * 24 * 60


def test_a_downtime_defers_the_whole_case_not_only_the_retry(con):
    """What an outage actually costs, measured rather than assumed.

    G9 does two things: it adds RETRY to `blocked_actions`, and it sets
    `wait_until`. `app/domain/engine.py:48` returns WAIT for any gate that set a
    `wait_until` in the future -- so during a downtime the case does not fall
    through to REMIND or PAY_LINK. It waits.

    That is deliberate and it predates this item: during an issuer outage the
    customer cannot pay on that rail, so a "pay now" message points at a dead one
    and burns a contact slot to say nothing. `downtime_backoff_minutes` is how long
    until we look again.

    It is written down here because the first draft of item 3b claimed the opposite
    -- that a downtime only took RETRY away and the rest of the ladder kept running
    -- and the test that "proved" it asserted on `legal_next_rungs`, which never
    sees the wait at all. The engine is the thing that decides, so the engine is
    what a test about deciding has to call.
    """
    import numpy as np
    from app.domain.engine import decide
    from app.domain.ladder import legal_next_rungs

    cid, post, row, w = worth_acting_on(con)

    before = decide(w.snapshot(row, NOW), CFG, post, np.random.default_rng(7))
    assert before.action is ActionType.RETRY, (
        "this case must want to act before the outage, or the test proves nothing")

    send(con, started())
    snap = w.snapshot(row, NOW)
    # The ladder does offer REMIND once RETRY is blocked, and REMIND is trained and
    # EV-positive here. So the ladder is not what stops it -- the wait is.
    assert ActionType.REMIND in legal_next_rungs(snap, run_gates(snap, CFG), CFG)

    during = decide(snap, CFG, post, np.random.default_rng(7))
    assert during.action is ActionType.WAIT
    assert during.stop_reason is StopReason.DOWNTIME
    assert during.execute_at == NOW + timedelta(minutes=CFG.downtime_backoff_minutes)

    send(con, resolved())
    after = decide(w.snapshot(row, NOW), CFG, post, np.random.default_rng(7))
    assert after.action is before.action, "resolving must give the case back"


def test_a_dropped_resolve_stalls_that_method_until_the_window_writes_it_off(con):
    """The cost of never expiring a row, stated in full and bounded.

    Because the wait is recomputed from `now` on every tick, a `.resolved` that
    never arrives does not merely block retries on that method -- it defers every
    case on it, fifteen minutes at a time, for as long as the case is alive. That is
    the real price of refusing to guess an end time.

    It does not hang forever, and the way it ends is the part worth knowing: after
    `window_hours` the recovery window closes and the case is WRITTEN OFF. So a
    dropped resolve webhook does not look like an outage. It looks like a rise in
    write-offs on one method a week later, which is far harder to notice -- and is
    the whole argument for making the open row loud on the attention list now.

    The alternative was worse in a quieter way still: ageing the row out would mean
    this system deciding an outage was over with no evidence, and sending customers
    at a dead rail silently.
    """
    import numpy as np
    from app.domain.engine import decide

    cid, post, row, w = worth_acting_on(con)

    send(con, started())
    # Whole days, so every probe lands at 14:00 and G12's quiet hours never take the
    # wait over from G9. A probe at 02:00 is deferred too -- but for a different
    # reason, and this test is about the downtime one.
    for hours in (0, 24, 144):
        later = NOW + timedelta(hours=hours)
        d = decide(w.snapshot(row, later), CFG, post, np.random.default_rng(7))
        assert d.action is ActionType.WAIT and d.stop_reason is StopReason.DOWNTIME, hours
        # Always fifteen more minutes, never "the outage has probably ended by now".
        assert d.execute_at == later + timedelta(minutes=CFG.downtime_backoff_minutes)

    # Past the window, the case is gone -- and not because of the downtime.
    dead = decide(w.snapshot(row, NOW + timedelta(hours=CFG.window_hours + 1)),
                  CFG, post, np.random.default_rng(7))
    assert dead.action is ActionType.WRITE_OFF
    assert dead.stop_reason is StopReason.WINDOW_EXPIRED

    # And the outage is still on the list a human reads, the whole time.
    assert ops.attention(con, NOW + timedelta(days=14))["counts"]["open_downtimes"] == 1


def test_the_ladder_itself_still_offers_the_other_rungs(con):
    """Separate from the test above, and a narrower claim than it looks.

    `legal_next_rungs` skips a gate-blocked rung rather than trapping the case, so
    the RETRY block does not empty the ladder. That matters the instant the outage
    resolves -- the case resumes at a real rung instead of being stuck at a dead
    one. It does NOT mean those rungs run during the outage; see above.
    """
    from app.domain.ladder import legal_next_rungs

    cid = case(con, method="netbanking")
    send(con, started())
    snap = snapshot_of(con, cid)
    g = run_gates(snap, CFG)

    legal = [a for a in legal_next_rungs(snap, g, CFG) if a != ActionType.WAIT]
    assert ActionType.RETRY not in legal
    assert legal, "an outage on one method must not empty the ladder"


# -- the demo endpoint -----------------------------------------------------

def test_the_demo_endpoint_uses_now_and_still_invents_no_end_time(con, monkeypatch):
    """`POST /api/demo/downtime`, which exists only because of the fixture epochs.

    Replaying fixture 05 works and the tests above rely on it, but its `begin` is a
    fixed February epoch, so on camera the Ops strip reads "191d 21h" -- true, and
    indistinguishable from a bug. This endpoint rewrites the two timestamps and
    nothing else.

    The thing to pin is that convenience did not smuggle in a forecast: a demo start
    must still store no end time, exactly as the real webhook does.
    """
    from app.api import dashboard

    monkeypatch.setattr(dashboard, "_con", con)

    out = dashboard.demo_downtime(method="upi", minutes_ago=4)
    assert out["action"] == "downtime_recorded"
    row = store.active_downtime(con, "upi")
    assert row["ends_at"] is None, "a demo outage must not know when it ends either"
    began = datetime.fromisoformat(row["began_at"])
    age = (datetime.now() - began).total_seconds() / 60
    assert 3 <= age <= 6, f"began_at should be ~4 minutes ago, got {age:.1f}m"

    # And the pairing works, so a demo can be undone on camera in one click.
    assert dashboard.demo_downtime(method="upi", resolve=True)["action"] == "downtime_resolved"
    assert store.active_downtime(con, "upi") is None


# -- and the benchmark is not wired to any of this -------------------------

def test_the_benchmark_does_not_read_the_downtime_table(con):
    """`sim/runner.py` gets downtime from the world, not from the live feed.

    The two paths are deliberately separate: `sim.world.in_downtime` is a simulated
    fact the benchmark's md5 depends on, and `store.active_downtime` is a live one.
    Wiring the live table into the benchmark would make the headline number depend
    on whatever webhooks happen to have arrived, and would move the md5 that
    `tests/test_live_worker.py` pins.
    """
    src = (ROOT / "sim" / "runner.py").read_text()
    for name in ("active_downtime", "open_downtimes", "downtimes"):
        assert name not in src, f"sim/runner.py now reads {name} from the live db"
    assert "in_downtime" in src, "the benchmark stopped modelling downtime at all"
