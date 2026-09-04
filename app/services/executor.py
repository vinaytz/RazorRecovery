"""
Executor port. The second of two swap points between benchmark and live.

`SandboxExecutor` backs the chaos demo: it can be told to time out, or to have
the obligation quietly settle mid-flight, so the failure modes are pressable
buttons rather than paragraphs in a README.
"""
from __future__ import annotations

from dataclasses import dataclass


class ExecutorTimeout(Exception):
    """The call did not come back. This is NOT a failure -- the money may have moved."""


@dataclass
class ExecResult:
    ok: bool
    detail: str = ""


class SandboxExecutor:
    def __init__(self, store_con):
        self.con = store_con
        self.force_timeout = False
        self.settle_midflight = False
        self.calls: list[str] = []

    # -- reads -------------------------------------------------------------

    def fetch_obligation(self, obligation_id: str) -> dict:
        """The last-second state re-check. Always called before acting."""
        r = self.con.execute(
            "SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
        if not r:
            return {"id": obligation_id, "settled": False, "amount_settled": 0}
        return {"id": r["id"], "settled": r["status"] == "SETTLED",
                "amount_settled": r["amount_settled"]}

    # -- writes ------------------------------------------------------------

    def execute(self, action_type: str, obligation_id: str, idem_key: str) -> ExecResult:
        self.calls.append(idem_key)

        if self.settle_midflight:
            # the customer paid while our action sat in the queue
            self.con.execute(
                "UPDATE obligations SET status='SETTLED', amount_settled=amount_due "
                "WHERE id = ?", (obligation_id,))
            self.con.commit()
            self.settle_midflight = False

        if self.force_timeout:
            self.force_timeout = False
            raise ExecutorTimeout("gateway did not respond")

        return ExecResult(ok=True, detail=f"{action_type} executed")


class RazorpayExecutor:
    """P7. Same two methods, talking to Razorpay test mode."""

    def __init__(self, client):
        self.client = client

    def fetch_obligation(self, obligation_id: str) -> dict:
        raise NotImplementedError("P7")

    def execute(self, action_type: str, obligation_id: str, idem_key: str) -> ExecResult:
        raise NotImplementedError("P7")
