"""Duplicate defence and the never-guess rule."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.controllers import execute as ex
from app.domain.models import ActionType
from app.repos import store
from app.services.executor import SandboxExecutor
from app.workers.reconciler import reconcile


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    c.execute("INSERT INTO obligations (id, customer_id, amount_due, amount_settled,"
              " status, opened_at) VALUES (?,?,?,?,?,?)",
              ("ob1", "cust1", 500_000, 0, "OPEN", datetime.now().isoformat()))
    c.commit()
    return c


def test_duplicate_event_inserts_once(con):
    for _ in range(3):
        con.execute("INSERT OR IGNORE INTO events "
                    "(dedupe_key, obligation_id, type, payload, received_at)"
                    " VALUES (?,?,?,?,?)", ("evt_1", "ob1", "payment.failed", "{}", "now"))
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_duplicate_action_is_ignored(con):
    at = datetime(2026, 3, 10, 12, 0, 0)
    first = ex.schedule(con, "case1", "ob1", ActionType.RETRY, at)
    second = ex.schedule(con, "case1", "ob1", ActionType.RETRY, at)
    assert first is not None and second is None
    assert con.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1


def test_settled_obligation_aborts_before_send(con):
    aid = ex.schedule(con, "c1", "ob1", ActionType.PAY_LINK, datetime.now())
    con.execute("UPDATE obligations SET status='SETTLED' WHERE id='ob1'")
    con.commit()
    row = con.execute("SELECT * FROM actions WHERE action_id=?", (aid,)).fetchone()
    out = ex.run(con, row, SandboxExecutor(con))
    assert out["status"] == "ABORTED"
    assert out["contact_sent"] is False       # nothing was sent. this is the point.


def test_timeout_becomes_unknown_not_failed(con):
    aid = ex.schedule(con, "c1", "ob1", ActionType.RETRY, datetime.now())
    e = SandboxExecutor(con)
    e.force_timeout = True
    row = con.execute("SELECT * FROM actions WHERE action_id=?", (aid,)).fetchone()
    out = ex.run(con, row, e)
    assert out["status"] == "UNKNOWN"

    r = reconcile(con, e)
    assert r["checked"] == 1
    assert con.execute("SELECT status FROM actions WHERE action_id=?",
                       (aid,)).fetchone()[0] in ("DONE", "FAILED")


# -- pins on two P0-P6 files changed in Batch 1 item 1a ---------------------
# Both changes are in `app/controllers/execute.py` / `SandboxExecutor.execute`.
# They exist so a failed live send does not spend a real customer's contact cap,
# and so the sandbox and the live executor share one call signature.

def test_the_sandbox_accepts_a_case_id(con):
    """`run()` passes `case_id=` so the live executor can attribute a contact.

    The sandbox must accept it too. The alternative -- catching TypeError at the
    call site to dispatch on arity -- would swallow a genuine TypeError raised
    *inside* the executor and retry the call.
    """
    e = SandboxExecutor(con)
    res = e.execute("PAY_LINK", "ob1", "idem-1", case_id="c1")
    assert res.ok is True
    assert e.execute("PAY_LINK", "ob1", "idem-2").ok is True   # still positional-only


def test_a_failed_send_is_not_counted_as_a_contact(con):
    """A link minted, an email that bounced: that is not a contact.

    Counting it would spend the customer's 7-day cap (G13) on a message nobody
    received, silencing us for a week over an SMTP outage. `run()` therefore
    trusts `ExecResult.contact_sent` over "is this a contact action".
    """
    from app.services.executor import ExecResult

    class SendFailed:
        def fetch_obligation(self, oid):
            return {"settled": False, "amount_due": 500_000}

        def execute(self, action_type, obligation_id, idem_key, case_id=None):
            return ExecResult(ok=False, detail="link minted | SMTP_FAILED",
                              contact_sent=False)

    aid = ex.schedule(con, "c1", "ob1", ActionType.PAY_LINK, datetime.now())
    row = con.execute("SELECT * FROM actions WHERE action_id=?", (aid,)).fetchone()
    out = ex.run(con, row, SendFailed())

    assert out["status"] == "FAILED"
    assert out["contact_sent"] is False        # PAY_LINK is a contact action; this was not a contact
    assert store.contacts_last_7d(con, "cust1", datetime.now()) == 0


def test_a_successful_send_is_counted(con):
    """The other half of the pin: `contact_sent=True` must survive `run()`."""
    from app.services.executor import ExecResult

    class SendOk:
        def fetch_obligation(self, oid):
            return {"settled": False, "amount_due": 500_000}

        def execute(self, action_type, obligation_id, idem_key, case_id=None):
            return ExecResult(ok=True, detail="link minted | sent", contact_sent=True)

    aid = ex.schedule(con, "c1", "ob1", ActionType.PAY_LINK, datetime.now())
    row = con.execute("SELECT * FROM actions WHERE action_id=?", (aid,)).fetchone()
    out = ex.run(con, row, SendOk())
    assert out["status"] == "DONE" and out["contact_sent"] is True


def test_an_executor_that_does_not_report_falls_back_to_the_action_type(con):
    """Backwards compatibility: `contact_sent=None` means "ask the action type".

    This is what keeps the benchmark's SandboxExecutor semantics unchanged.
    """
    from app.services.executor import ExecResult

    class Quiet:
        def fetch_obligation(self, oid):
            return {"settled": False, "amount_due": 500_000}

        def execute(self, action_type, obligation_id, idem_key, case_id=None):
            return ExecResult(ok=True, detail="did the thing")   # contact_sent unset

    aid = ex.schedule(con, "c1", "ob1", ActionType.REMIND, datetime.now())
    row = con.execute("SELECT * FROM actions WHERE action_id=?", (aid,)).fetchone()
    assert ex.run(con, row, Quiet())["contact_sent"] is True

    aid2 = ex.schedule(con, "c1", "ob1", ActionType.RETRY, datetime.now())
    row2 = con.execute("SELECT * FROM actions WHERE action_id=?", (aid2,)).fetchone()
    assert ex.run(con, row2, Quiet())["contact_sent"] is False   # RETRY reaches nobody
