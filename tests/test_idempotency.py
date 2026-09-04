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
    c.execute("INSERT INTO obligations VALUES (?,?,?,?,?,?)",
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
