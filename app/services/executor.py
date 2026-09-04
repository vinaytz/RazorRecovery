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
    """Razorpay test mode. Same two methods as SandboxExecutor -- that is the port.

    Nothing else in the system changes when you swap this in. `controllers/execute`
    calls `fetch_obligation` then `execute`, and does not know or care which of the
    two it is holding.

    DECISION: this executor never calls a charge API. It reads state and it
    schedules contact. Creating a payment link is reversible and rate-limited; a
    server-initiated debit on someone's card from a hackathon build is not. RETRY
    is therefore recorded as INTENT_ONLY rather than executed live, and the
    response says so rather than reporting a success that did not happen.
    """

    def __init__(self, client, dry_run: bool = True):
        self.client = client
        self.dry_run = dry_run          # DRY_RUN=true is the default. SPEC rule 6.
        self.calls: list[str] = []

    # -- reads -------------------------------------------------------------

    def fetch_obligation(self, obligation_id: str) -> dict:
        """The last-second re-check, against Razorpay rather than sqlite.

        The obligation id is an order / invoice / subscription id, so we ask the
        matching endpoint. A read failure returns `settled: False` with a detail
        string: unknown state must never look like "settled" (that would silently
        abort real recovery work) nor crash the caller.
        """
        try:
            if obligation_id.startswith("order_"):
                o = self.client.order.fetch(obligation_id)
                paid = int(o.get("amount_paid") or 0)
                total = int(o.get("amount") or 0)
                return {"id": obligation_id,
                        "settled": o.get("status") == "paid" or (total > 0 and paid >= total),
                        "amount_settled": paid}
            if obligation_id.startswith("inv_"):
                i = self.client.invoice.fetch(obligation_id)
                paid = int(i.get("amount_paid") or 0)
                return {"id": obligation_id, "settled": i.get("status") == "paid",
                        "amount_settled": paid}
            if obligation_id.startswith("sub_"):
                s = self.client.subscription.fetch(obligation_id)
                return {"id": obligation_id,
                        "settled": s.get("status") in ("active", "completed"),
                        "amount_settled": 0}
            p = self.client.payment.fetch(obligation_id)
            return {"id": obligation_id, "settled": p.get("status") == "captured",
                    "amount_settled": int(p.get("amount") or 0) if p.get("captured") else 0}
        except Exception as e:                       # noqa: BLE001
            return {"id": obligation_id, "settled": False, "amount_settled": 0,
                    "detail": f"fetch failed: {type(e).__name__}: {e}"}

    # -- writes ------------------------------------------------------------

    def execute(self, action_type: str, obligation_id: str, idem_key: str) -> ExecResult:
        self.calls.append(idem_key)

        if self.dry_run:
            return ExecResult(ok=True, detail=f"DRY_RUN: would {action_type} on {obligation_id}")

        if action_type == "RETRY":
            # See the class docstring. We record the intent; we do not debit.
            return ExecResult(ok=False, detail="INTENT_ONLY: server-initiated debit not enabled")

        if action_type in ("REMIND", "PAY_LINK", "METHOD_CHANGE"):
            try:
                link = self.client.payment_link.create({
                    "amount": 0, "currency": "INR",
                    "description": f"Complete your payment ({action_type})",
                    "reference_id": idem_key,       # Razorpay rejects a repeat. idempotency.
                    "notify": {"sms": False, "email": False},
                    "reminder_enable": False,
                })
                return ExecResult(ok=True, detail=f"payment_link {link.get('id')}")
            except Exception as e:                   # noqa: BLE001
                return ExecResult(ok=False, detail=f"{type(e).__name__}: {e}")

        return ExecResult(ok=False, detail=f"{action_type} not executable via API")
