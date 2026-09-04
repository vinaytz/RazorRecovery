"""
The execution path. Four rules, each learned the expensive way by somebody.

1. RE-CHECK STATE FIRST. A decision made six hours ago is a proposal, not a
   permission. The customer may have paid in the meantime. You can abort a
   retry; you cannot unsend an SMS.
2. IDEMPOTENCY KEY ON EVERY ACTION. Two webhooks must never become two retries.
3. A TIMEOUT IS `UNKNOWN`, NEVER A FAILURE. The money may have moved. Guessing
   "failed" is how customers get charged twice.
4. MARK IN_FLIGHT BEFORE THE CALL, not after.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from app.domain.models import CONTACT_ACTIONS, ActionType
from app.services.executor import ExecutorTimeout


def schedule(con, case_id: str, obligation_id: str, action: ActionType,
             execute_at: datetime, idem_key: str | None = None) -> str | None:
    """Returns the action id, or None if this action already exists (duplicate)."""
    key = idem_key or f"{case_id}:{action.value}:{execute_at.isoformat()}"
    aid = f"act_{uuid.uuid4().hex[:12]}"
    cur = con.execute(
        "INSERT OR IGNORE INTO actions "
        "(action_id, case_id, obligation_id, type, execute_at, status, idem_key, "
        " attempts, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, case_id, obligation_id, action.value, execute_at.isoformat(),
         "PENDING", key, 0, None, datetime.now().isoformat()))
    con.commit()
    return aid if cur.rowcount else None      # rowcount 0 == duplicate, ignored


def run(con, action_row, executor) -> dict:
    aid = action_row["action_id"]
    atype = ActionType(action_row["type"])
    obligation_id = action_row["obligation_id"]

    # 1. re-check. always. first.
    fresh = executor.fetch_obligation(obligation_id)
    if fresh["settled"]:
        _set(con, aid, "ABORTED", "ALREADY_SETTLED")
        return {"action_id": aid, "status": "ABORTED", "reason": "ALREADY_SETTLED",
                "contact_sent": False,
                "note": "customer had already paid -- nothing was sent"}

    # 4. in-flight before the call
    _set(con, aid, "IN_FLIGHT", None)

    try:
        res = executor.execute(atype.value, obligation_id, action_row["idem_key"],
                               case_id=action_row["case_id"])
    except ExecutorTimeout as e:
        # 3. never guess.
        _set(con, aid, "UNKNOWN", str(e))
        return {"action_id": aid, "status": "UNKNOWN", "reason": str(e),
                "contact_sent": atype in CONTACT_ACTIONS,
                "note": "state unknown -- reconciler will resolve, no assumption made"}

    # CORRECTNESS: ask the executor whether a human was actually reached, and only
    # fall back to "is this a contact action" when it does not say. On the live path
    # a payment link can be minted and the email still fail -- counting that as a
    # contact would spend the customer's 7-day cap (gate G13) on a message nobody
    # received, silencing us for a week over an SMTP outage.
    sent = res.contact_sent if getattr(res, "contact_sent", None) is not None \
        else atype in CONTACT_ACTIONS

    _set(con, aid, "DONE" if res.ok else "FAILED", res.detail)
    return {"action_id": aid, "status": "DONE" if res.ok else "FAILED",
            "reason": res.detail, "contact_sent": sent}


def due_actions(con, now: datetime | None = None) -> list:
    now = now or datetime.now()
    return list(con.execute(
        "SELECT * FROM actions WHERE status = 'PENDING' AND execute_at <= ? "
        "ORDER BY execute_at LIMIT 50", (now.isoformat(),)))


def _set(con, action_id: str, status: str, detail: str | None) -> None:
    con.execute("UPDATE actions SET status = ?, detail = ?, attempts = attempts + 1 "
                "WHERE action_id = ?", (status, detail, action_id))
    con.commit()
