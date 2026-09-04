"""
Resolves UNKNOWN actions by asking the source of truth who won.

Without this, every timeout is a permanent unknown and the ledger drifts. With
it, "we don't know yet" is a real, temporary, self-clearing state -- which is
what it is in every payment system that works.
"""
from __future__ import annotations


def reconcile(con, executor, limit: int = 50) -> dict:
    rows = list(con.execute(
        "SELECT * FROM actions WHERE status = 'UNKNOWN' LIMIT ?", (limit,)))
    resolved, still_unknown = [], []

    for r in rows:
        fresh = executor.fetch_obligation(r["obligation_id"])
        if fresh["settled"]:
            con.execute("UPDATE actions SET status='DONE', detail='reconciled: settled' "
                        "WHERE action_id = ?", (r["action_id"],))
            resolved.append({"action_id": r["action_id"], "resolved_to": "DONE"})
        else:
            con.execute("UPDATE actions SET status='FAILED', detail='reconciled: not settled' "
                        "WHERE action_id = ?", (r["action_id"],))
            resolved.append({"action_id": r["action_id"], "resolved_to": "FAILED"})
    con.commit()
    return {"checked": len(rows), "resolved": resolved, "still_unknown": still_unknown}
