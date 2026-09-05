"""
Operator control: the stop button, the mode toggle, and the merchant's window.

The load-bearing test in this file is `test_pause_stops_deciding_but_not_ingesting`.
Everything else guards a promise made on the Ops tab; that one guards the promise
that makes the button safe to press. An engine that stops listening when it is
paused comes back to a ledger that drifted while it was off, and the operator has
no way to know what they missed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.config_loader import load_config
from app.controllers import execute as ex
from app.controllers import ingest as ingest_ctl
from app.domain.gates import run_gates
from app.domain.models import (ActionType, Arm, CaseSnapshot, FailureClass,
                               ObligationKind)
from app.repos import store
from app.services import ops
from app.services.executor import ExecResult
from app.workers.live import LiveWorker

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


class FakeExecutor:
    """Records what it was asked to do. Nothing leaves the process."""

    dry_run = True

    def __init__(self):
        self.calls: list[tuple] = []

    def fetch_obligation(self, oid):
        return {"settled": False, "amount_due": 499_900}

    def execute(self, action_type, obligation_id, idem_key, case_id=None):
        self.calls.append((action_type, obligation_id, idem_key))
        return ExecResult(ok=True, detail=f"{action_type} done", contact_sent=True)


def case(c, *, oid="order_O1", amount=499_900, rung=0, attempts=1,
         customer="cust_O1", status="OPEN", kind=ObligationKind.ORDER):
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
               FailureClass.INSUFFICIENT_FUNDS.value, "card", kind.value, rung,
               attempts, status, 0, 0, opened.isoformat(), None))
    c.commit()
    return cid


def failed_payment(oid="order_INGEST", pid="pay_INGEST", amount=250_000):
    return {"entity": "event", "event": "payment.failed", "contains": ["payment"],
            "id": f"evt_{pid}",
            "payload": {"payment": {"entity": {
                "id": pid, "entity": "payment", "amount": amount, "currency": "INR",
                "status": "failed", "order_id": oid, "method": "card",
                "customer_id": "cust_INGEST", "email": "b@example.com",
                "contact": "+919000000002",
                "error_description": "Your card has insufficient balance."}}}}


def snapshot_at(when: datetime) -> CaseSnapshot:
    """A plain snapshot at a given wall-clock hour. Only `now` matters here."""
    return CaseSnapshot(
        case_id="c1", obligation_id="order_1", customer_id="cust_1",
        merchant_id="merchant_1", arm=Arm.ENGINE, amount_due=500_000, amount_settled=0,
        kind=ObligationKind.ORDER, currency="INR",
        failure_class=FailureClass.INSUFFICIENT_FUNDS, method="card", is_mandate=False,
        now=when, opened_at=when - timedelta(hours=2), attempts=0, rung=0,
        last_action_at=None, promised_until=None, customer_tenure_days=100,
        customer_past_failures=1, customer_past_recoveries=0, contacts_last_7d=0,
        opted_out=False, risk_blocked=False, last_notice_sent_at=None, afa_valid=True,
        obligation_settled=False, method_in_downtime=False, downtime_ends_at=None,
        pending_action_types=())


# -- THE PROMISE THAT MAKES THE BUTTON SAFE --------------------------------

def test_pause_stops_deciding_but_not_ingesting(con):
    """The whole point. Decisions freeze; the ledger keeps up with the world."""
    case(con)
    ops.pause(con, ops.GLOBAL, reason="incident", who="asha", now=NOW)

    w = LiveWorker(con, FakeExecutor())
    out = w.tick(NOW)

    # Nothing decided. Every case reports WHY, and the reason is a human's.
    assert all(d["paused"] for d in out["decided"])
    assert all(d["stop_reason"] == "PAUSED_GLOBAL" for d in out["decided"])
    assert con.execute("SELECT COUNT(*) FROM decisions WHERE run_id='live'"
                       ).fetchone()[0] == 0
    assert out["executed"] == []

    # But the webhook path is untouched: the event lands, the case opens.
    before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    r = ingest_ctl.ingest(con, failed_payment(), {}, now=NOW)
    assert r["applied"] is True
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1
    assert con.execute("SELECT COUNT(*) FROM cases WHERE case_id = ?",
                       ("live_order_INGEST",)).fetchone()[0] == 1


def test_pause_cancels_what_was_already_queued(con):
    """Freezing new decisions while a queued PAY_LINK still fires is not a pause."""
    cid = case(con)
    aid = ex.schedule(con, cid, "order_O1", ActionType.PAY_LINK, NOW - timedelta(minutes=1))
    assert aid

    out = ops.pause(con, ops.GLOBAL, reason="stop", who="asha", now=NOW)
    assert out["cancelled"] == 1

    row = con.execute("SELECT status, detail FROM actions WHERE action_id = ?",
                      (aid,)).fetchone()
    assert row["status"] == "CANCELLED"
    assert "CANCELLED_BY_PAUSE" in row["detail"] and "asha" in row["detail"]

    # And it does not execute on the next tick either.
    fx = FakeExecutor()
    LiveWorker(con, fx).tick(NOW)
    assert fx.calls == []


def test_in_flight_actions_are_left_alone(con):
    """Mid-call is exactly the state we refuse to guess about. Rule 3 of execute.py."""
    cid = case(con)
    aid = ex.schedule(con, cid, "order_O1", ActionType.REMIND, NOW)
    con.execute("UPDATE actions SET status='IN_FLIGHT' WHERE action_id=?", (aid,))
    con.commit()
    out = ops.pause(con, ops.GLOBAL, who="asha", now=NOW)
    assert out["cancelled"] == 0
    assert con.execute("SELECT status FROM actions WHERE action_id=?",
                       (aid,)).fetchone()[0] == "IN_FLIGHT"


def test_resume_lets_decisions_flow_again(con):
    case(con)
    ops.pause(con, ops.GLOBAL, who="asha", now=NOW)
    w = LiveWorker(con, FakeExecutor())
    assert all(d.get("paused") for d in w.tick(NOW)["decided"])

    ops.resume(con, who="asha", now=NOW)
    out = w.tick(NOW)
    assert out["decided"] and not any(d.get("paused") for d in out["decided"])
    assert con.execute("SELECT COUNT(*) FROM decisions WHERE run_id='live'"
                       ).fetchone()[0] >= 1


def test_a_lifted_pause_stays_in_the_table(con):
    """"Recovery was stopped between 14:02 and 14:19, by whom" is the first question
    after an incident, and a boolean cannot answer it."""
    ops.pause(con, ops.GLOBAL, reason="issuer outage", who="asha", now=NOW)
    ops.resume(con, who="ravi", now=NOW + timedelta(minutes=17))
    r = con.execute("SELECT * FROM pauses").fetchone()
    assert r["who"] == "asha" and r["reason"] == "issuer outage"
    assert r["lifted_by"] == "ravi" and r["lifted_at"].startswith("2026-03-10T12:17")


def test_pressing_pause_twice_is_not_two_pauses(con):
    ops.pause(con, ops.GLOBAL, who="asha", now=NOW)
    second = ops.pause(con, ops.GLOBAL, who="asha", now=NOW)
    assert second["already"] is True
    assert len(ops.pause_set(con).rows) == 1


# -- SCOPES ----------------------------------------------------------------

def test_action_scope_stops_one_action_type_only(con):
    ps = ops.PauseSet(())
    ops.pause(con, ops.ACTION, "PAY_LINK", who="asha", now=NOW)
    ps = ops.pause_set(con)
    assert ps.blocks_action("PAY_LINK") == "PAUSED_ACTION:PAY_LINK"
    assert ps.blocks_action("REMIND") is None
    assert ps.blocks_case("merchant_1", 0) is None      # deciding still allowed


def test_action_scope_cancels_only_that_type(con):
    cid = case(con)
    keep = ex.schedule(con, cid, "order_O1", ActionType.REMIND, NOW)
    kill = ex.schedule(con, cid, "order_O1", ActionType.PAY_LINK, NOW)
    ops.pause(con, ops.ACTION, "PAY_LINK", who="asha", now=NOW)
    assert con.execute("SELECT status FROM actions WHERE action_id=?",
                       (kill,)).fetchone()[0] == "CANCELLED"
    assert con.execute("SELECT status FROM actions WHERE action_id=?",
                       (keep,)).fetchone()[0] == "PENDING"


def test_a_due_action_of_a_paused_type_is_cancelled_not_executed(con):
    """The pause was raised between scheduling and the tick that would run it."""
    cid = case(con)
    aid = ex.schedule(con, cid, "order_O1", ActionType.REMIND, NOW - timedelta(minutes=1))
    con.execute("INSERT INTO pauses (scope, value, who, created_at) VALUES (?,?,?,?)",
                (ops.ACTION, "REMIND", "asha", NOW.isoformat()))
    con.commit()                                   # raised WITHOUT the cancel sweep
    fx = FakeExecutor()
    out = LiveWorker(con, fx).execute_due(NOW)
    assert fx.calls == []
    assert [o["status"] for o in out] == ["CANCELLED"]
    assert con.execute("SELECT status FROM actions WHERE action_id=?",
                       (aid,)).fetchone()[0] == "CANCELLED"


def test_rung_scope_holds_the_top_of_the_ladder(con):
    low = case(con, oid="order_LOW", rung=1, customer="cust_LOW")
    high = case(con, oid="order_HIGH", rung=4, customer="cust_HIGH")
    ops.pause(con, ops.RUNG, "4", reason="review escalations", who="asha", now=NOW)
    ps = ops.pause_set(con)
    assert ps.blocks_case("merchant_1", 4) == "PAUSED_RUNG>=4"
    assert ps.blocks_case("merchant_1", 1) is None

    out = LiveWorker(con, FakeExecutor()).tick(NOW)
    by_case = {d["case_id"]: d for d in out["decided"]}
    assert by_case[high].get("paused") is True
    assert by_case[low].get("paused") is not True


def test_merchant_scope_matches_the_snapshot_merchant(con):
    ops.pause(con, ops.MERCHANT, "merchant_1", who="asha", now=NOW)
    ps = ops.pause_set(con)
    assert ps.blocks_case("merchant_1", 0) == "PAUSED_MERCHANT:merchant_1"
    assert ps.blocks_case("merchant_2", 0) is None


def test_a_scoped_pause_needs_a_value(con):
    with pytest.raises(ValueError):
        ops.pause(con, ops.ACTION, None, who="asha", now=NOW)


def test_every_block_is_a_reason_string_never_a_bool(con):
    """Hard rule 3: every STOP carries a reason code."""
    ops.pause(con, ops.GLOBAL, who="asha", now=NOW)
    ps = ops.pause_set(con)
    for got in (ps.blocks_case("merchant_1", 0), ps.blocks_action("REMIND")):
        assert isinstance(got, str) and got


# -- DRY RUN <-> LIVE ------------------------------------------------------

def test_mode_defaults_to_the_environment(con, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    m = ops.effective_dry_run(con)
    assert m == {"dry_run": True, "source": "env", "env_default": True}


def test_operator_override_beats_the_environment_and_says_so(con, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    ops.set_dry_run(con, False, who="asha", note="filming")
    m = ops.effective_dry_run(con)
    assert m["dry_run"] is False and m["source"] == "operator"
    assert m["env_default"] is True          # the env is still visible, not overwritten


def test_clearing_the_override_falls_back_to_the_environment(con, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    ops.set_dry_run(con, True, who="asha")
    assert ops.effective_dry_run(con)["dry_run"] is True
    ops.set_dry_run(con, None, who="asha")
    m = ops.effective_dry_run(con)
    assert m["dry_run"] is False and m["source"] == "env"


def test_the_worker_follows_the_toggle_without_a_restart(con, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    fx = FakeExecutor()
    w = LiveWorker(con, fx)
    assert fx.dry_run is True
    ops.set_dry_run(con, False, who="asha")
    w.tick(NOW)
    assert fx.dry_run is False


def test_going_live_requires_a_confirm(con):
    from fastapi import HTTPException

    from app.api import ops as ops_api
    ops_api.dashboard._con = con                       # route the API at the temp db
    try:
        with pytest.raises(HTTPException) as e:
            ops_api.mode(dry_run=False, confirm=False)
        assert e.value.status_code == 400
        assert "what_this_changes" in e.value.detail
        assert ops_api.mode(dry_run=False, confirm=True)["dry_run"] is False
    finally:
        ops_api.dashboard._con = None


def test_live_readiness_does_not_claim_a_send_it_cannot_make(monkeypatch):
    """Telling an operator "you are now live" when no SMTP host exists is a lie."""
    from app.api import ops as ops_api
    for k in ("SMTP_HOST", "SMTP_FROM", "SMTP_USER", "RAZORPAY_KEY_ID",
              "RAZORPAY_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    r = ops_api.live_readiness()
    assert r["will_actually_send"] is False
    assert r["summary"] == "nothing can leave this process yet"
    assert any("SMTP_HOST" in g for g in r["gaps"])


# -- MERCHANT-CONFIGURABLE QUIET HOURS -------------------------------------
# G12 is NOT weakened. The gate still reads cfg.quiet_start/cfg.quiet_end off the
# config it is handed; only which config that is has changed.

def test_quiet_hours_defaults_to_the_file(con):
    q = ops.quiet_hours(con, "merchant_1")
    base = load_config()
    assert (q["start"], q["end"]) == (base.quiet_start, base.quiet_end)
    assert q["source"] == "config/default.yaml"


def test_an_override_changes_the_config_the_gate_sees(con):
    """The 24/7 gaming merchant. Same gate, different window."""
    ops.set_quiet_hours(con, "merchant_1", 2, 5, who="asha")
    cfg = ops.config_for(con, "merchant_1")
    assert (cfg.quiet_start, cfg.quiet_end) == (2, 5)
    # and the file itself is untouched
    assert (load_config().quiet_start, load_config().quiet_end) == (21, 9)


def test_the_gate_itself_is_unchanged(con):
    """22:00 blocks under the file window and passes under the merchant's.

    Same `run_gates`, same G12, two configs. This is what "do not weaken the gate"
    looks like as a test.
    """
    late = snapshot_at(datetime(2026, 3, 10, 22, 0, 0))

    file_cfg = load_config()
    ops.set_quiet_hours(con, "merchant_1", 2, 5, who="asha")
    merchant_cfg = ops.config_for(con, "merchant_1")

    def g12(cfg):
        return [g for g in run_gates(late, cfg).trace if g.gate == "G12_QUIET_HOURS"][0]

    assert g12(file_cfg).passed is False
    assert g12(merchant_cfg).passed is True
    # ...and the merchant's own night still blocks. The window moved; it did not go.
    assert g12(ops.config_for(con, "merchant_1")).passed is True
    night = snapshot_at(datetime(2026, 3, 10, 3, 0, 0))
    assert [g for g in run_gates(night, merchant_cfg).trace
            if g.gate == "G12_QUIET_HOURS"][0].passed is False


def test_an_override_gets_its_own_config_version(con):
    """CORRECTNESS. `config_loader._CACHE` is keyed on version and `version()` is
    what replay uses to rebuild the config a decision was made under. Without a
    distinct version an override would overwrite the cached "v1" and replay would
    re-decide old cases under settings that did not exist when they were made."""
    from app import config_loader

    base = load_config()
    over = load_config(**{"compliance.quiet_hours.start": 2})
    assert base.version == "v1"
    assert over.version != base.version and over.version.startswith("v1+")
    assert config_loader.version("v1").quiet_start == base.quiet_start
    assert config_loader.version(over.version).quiet_start == 2
    # Same overlay, same version -- a stored config_version stays resolvable.
    assert load_config(**{"compliance.quiet_hours.start": 2}).version == over.version


def test_no_override_means_the_plain_config_object(con):
    """The common path must be byte-identical to what it was before ops existed."""
    assert ops.config_for(con, "merchant_1").version == "v1"


@pytest.mark.parametrize("start,end", [(-1, 9), (24, 9), (21, 99)])
def test_quiet_hours_outside_0_23_are_refused(con, start, end):
    with pytest.raises(ValueError):
        ops.set_quiet_hours(con, "merchant_1", start, end, who="asha")


def test_resetting_goes_back_to_the_file(con):
    ops.set_quiet_hours(con, "merchant_1", 2, 5, who="asha")
    all_ = dict(ops.quiet_hours_all(con))
    all_.pop("merchant_1")
    ops.set_setting(con, ops.K_QUIET, all_, who="asha")
    assert ops.quiet_hours(con, "merchant_1")["source"] == "config/default.yaml"


# -- THE AUDIT TRAIL -------------------------------------------------------

def test_every_change_records_what_it_replaced(con):
    ops.set_quiet_hours(con, "merchant_1", 21, 9, who="asha")
    ops.set_quiet_hours(con, "merchant_1", 2, 5, who="ravi", note="24/7 merchant")
    rows = ops.audit(con, 10)
    latest = rows[0]
    assert latest["who"] == "ravi" and latest["note"] == "24/7 merchant"
    assert latest["before"]["merchant_1"] == {"start": 21, "end": 9}
    assert latest["after"]["merchant_1"] == {"start": 2, "end": 5}


def test_pause_and_resume_are_audited(con):
    ops.pause(con, ops.ACTION, "PAY_LINK", reason="issuer flaky", who="asha", now=NOW)
    ops.resume(con, who="asha", now=NOW)
    keys = [r["key"] for r in ops.audit(con, 10)]
    assert "resume" in keys and "pause.action" in keys


# -- TODAY, AND WHAT NEEDS A HUMAN -----------------------------------------

def test_today_counts_money_in_and_money_spent(con):
    cid = case(con)
    now = datetime.now()
    con.execute("INSERT INTO actions (action_id, case_id, obligation_id, type,"
                " execute_at, status, idem_key, attempts, detail, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("act_t1", cid, "order_O1", "PAY_LINK", now.isoformat(), "DONE",
                 "k1", 1, None, now.isoformat()))
    store.record_contact(con, customer_id="cust_O1", obligation_id="order_O1",
                         case_id=cid, channel="email", action="PAY_LINK", tier="static",
                         used_llm=False, subject="s", sent_at=now.isoformat(),
                         ok=True, detail="d")
    store.record_settlement(con, payment_id="pay_t1", obligation_id="order_O1",
                            case_id=cid, amount=499_900, method="upi", match_level=1,
                            match_basis="order_id", match_confidence="certain",
                            match_evidence="e", candidates=1, attributed=True,
                            attribution_reason="RECOVERED", settled_at=now.isoformat(),
                            source="webhook")
    t = ops.today(con, now)
    assert t["actions_executed"] == 1 and t["contacts"] == 1
    assert t["recovered"] == 499_900 and t["recovered_attributed"] == 499_900
    assert t["budget_used"] == load_config().action_cost["PAY_LINK"]


def test_a_case_asked_twice_is_still_one_case(con):
    """Both numbers, because either one alone misleads.

    A waiting case is re-decided on every tick, so "decisions today" runs into the
    thousands on a handful of cases. Showing it without the case count reads as
    activity that is not there; showing only the case count hides the churn.
    """
    cid = case(con)
    now = datetime.now()
    store.save_decisions(con, [
        (f"dec_x{i}", "live", cid, now.isoformat(), "WAIT", None,
         "{}", "[]", "[]", "v1", "waiting", None) for i in range(3)])
    t = ops.today(con, now)
    assert t["decisions"] == 3
    assert t["cases_decided"] == 1


def test_attention_finds_the_four_things_that_need_a_human(con):
    cid = case(con, oid="order_TOP", rung=6, customer="cust_TOP")
    now = datetime.now()
    for aid, status, atype in (("a_unk", "UNKNOWN", "RETRY"), ("a_fail", "FAILED", "RETRY")):
        con.execute("INSERT INTO actions (action_id, case_id, obligation_id, type,"
                    " execute_at, status, idem_key, attempts, detail, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (aid, cid, "order_TOP", atype, now.isoformat(), status, aid, 1,
                     "INTENT_ONLY: server-initiated debit not enabled", now.isoformat()))
    con.commit()
    for i in range(load_config().max_contacts_7d):
        store.record_contact(con, customer_id="cust_TOP", obligation_id="order_TOP",
                             case_id=cid, channel="email", action="REMIND",
                             tier="static", used_llm=False, subject="s",
                             sent_at=now.isoformat(), ok=True, detail="d")

    a = ops.attention(con, now)
    assert a["counts"]["unknown_actions"] == 1
    assert a["counts"]["failed_actions"] == 1
    assert a["counts"]["at_contact_cap"] == 1
    assert a["counts"]["ladder_top"] == 1
    # The FAILED row is the gap the 3a before-shot found: a burnt rung, silent.
    assert "INTENT_ONLY" in a["failed_actions"][0]["detail"]


def test_a_failed_send_does_not_put_a_customer_at_the_cap(con):
    """Same rule as G13. An SMTP outage must not read as an over-contacted customer."""
    cid = case(con)
    now = datetime.now()
    for _ in range(5):
        store.record_contact(con, customer_id="cust_O1", obligation_id="order_O1",
                             case_id=cid, channel="email", action="REMIND", tier="static",
                             used_llm=False, subject="s", sent_at=now.isoformat(),
                             ok=False, detail="SMTP_FAILED")
    assert ops.attention(con, now)["counts"]["at_contact_cap"] == 0


def test_state_survives_an_empty_database(con):
    """The Ops tab is the first thing an operator opens on a fresh clone."""
    from app.api import ops as ops_api
    ops_api.dashboard._con = con
    try:
        s = ops_api.state()
        assert s["paused"] is False and s["pauses"] == []
        assert s["today"]["decisions"] == 0
        assert s["config"]["effective_version"] == "v1"
        assert json.dumps(s, default=str)          # serialisable, so the tab renders
    finally:
        ops_api.dashboard._con = None
