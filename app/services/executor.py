"""
Executor port. The second of two swap points between benchmark and live.

`SandboxExecutor` backs the chaos demo: it can be told to time out, or to have
the obligation quietly settle mid-flight, so the failure modes are pressable
buttons rather than paragraphs in a README.

`RazorpayExecutor` is the live half. The one rule that governs all of it:

    WE ARE A LISTENER AND A NUDGER. WE ARE NEVER IN THE MERCHANT'S PAYMENT PATH.

Concretely, we never call `order.create`. The merchant already has an order for
this debt; their fulfilment, their reconciliation and their ledger are all keyed
on it. If we minted our own order, the customer's money would land against an id
the merchant's backend has never heard of, and the goods would never ship. A
recovery tool that loses the merchant's order is worse than no recovery tool.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.repos import store

log = logging.getLogger("razorrecovery.executor")

# Actions that put a payable URL in front of a customer. RETRY is not here: it is
# a silent server-side debit and we do not issue those (see `execute`).
LINK_ACTIONS = frozenset({"REMIND", "PAY_LINK", "METHOD_CHANGE"})

# Razorpay's documented limits on the Create Payment Link API.
REFERENCE_ID_MAX = 40            # "must be no more than 40"
EXPIRE_BY_FLOOR = timedelta(minutes=16)   # API rejects < 15 min in the future
EXPIRE_BY_CEILING = timedelta(days=180)   # API rejects > 6 months out


class ExecutorTimeout(Exception):
    """The call did not come back. This is NOT a failure -- the money may have moved."""


@dataclass
class ExecResult:
    ok: bool
    detail: str = ""


def order_id_of(obligation_id: str) -> str | None:
    """The merchant's existing order id, or None when the debt is not an order.

    An invoice or a subscription cycle has no order to reuse. Returning None here
    rather than inventing one is the whole point: a missing order is a fact to
    record, not a gap to fill.
    """
    return obligation_id if obligation_id.startswith("order_") else None


def reference_id_for(obligation_id: str, action: str) -> str:
    """`<obligation_id>#<ACTION>`, capped at Razorpay's 40 characters.

    DECISION: the brief asks for `reference_id = obligation_id` so the callback is
    traceable. Razorpay additionally requires reference_id to be unique per link
    ("payment link creation with reference ID already attempted", 400), so a bare
    obligation_id allows exactly one link per debt for all time -- the second
    action on the same obligation would be rejected. Prefixing with the
    obligation_id keeps traceability (`obligation_from_reference_id` is a split)
    while making Razorpay's own uniqueness rule enforce precisely the
    (obligation_id, action) idempotency scope we want. One rule, two guarantees.
    """
    ref = f"{obligation_id}#{action}"
    if len(ref) <= REFERENCE_ID_MAX:
        return ref
    # Pathological id length. Keep the readable prefix, hash the rest, stay traceable
    # enough to grep -- and never silently send a 41-character reference_id.
    digest = hashlib.sha1(ref.encode()).hexdigest()[:8]
    return f"{obligation_id[:REFERENCE_ID_MAX - 9]}#{digest}"


def obligation_from_reference_id(reference_id: str) -> str:
    """The inverse. This is what makes a payment-link callback attributable."""
    return (reference_id or "").split("#", 1)[0]


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

    def __init__(self, client, con=None, dry_run: bool = True, clock=None,
                 window_hours: int = 168, merchant_name: str = "the merchant"):
        self.client = client            # None means "no credentials" -- stub, never crash.
        self.con = con                  # None means "no store" -- no idempotency, loud log.
        self.dry_run = dry_run          # DRY_RUN=true is the default. Invariant 6.
        self.clock = clock
        self.window_hours = window_hours
        self.merchant_name = merchant_name
        self.calls: list[str] = []

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now()

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

    def _obligation_row(self, obligation_id: str) -> dict:
        if self.con is None:
            return {}
        try:
            r = self.con.execute("SELECT * FROM obligations WHERE id = ?",
                                 (obligation_id,)).fetchone()
            return dict(r) if r else {}
        except Exception as e:                       # noqa: BLE001
            log.warning("obligation read failed for %s: %s", obligation_id, e)
            return {}

    def _expire_by(self, now: datetime, opened_at: str | None,
                   window_ends_at: datetime | None) -> datetime:
        """The link dies when the recovery window does, clamped to Razorpay's limits.

        A link that outlives the recovery window is a debt we have already given up
        on, still collectable by a customer who finds an old SMS -- money arriving
        against a written-off case that nothing is watching. So the deadline on the
        link is the same deadline the ladder runs on.
        """
        end = window_ends_at
        if end is None:
            start = now
            if opened_at:
                try:
                    start = datetime.fromisoformat(opened_at)
                except ValueError:
                    pass
            end = start + timedelta(hours=self.window_hours)
        # The API rejects < 15 minutes out. A window with 3 minutes left is a real
        # state, so clamp rather than fail: a short link beats no link.
        return max(min(end, now + EXPIRE_BY_CEILING), now + EXPIRE_BY_FLOOR)

    def create_payment_link(self, obligation_id: str, amount: int | None = None,
                            contact: str | None = None, email: str | None = None, *,
                            action: str = "PAY_LINK", name: str | None = None,
                            window_ends_at: datetime | None = None,
                            description: str | None = None) -> dict:
        """One payable URL for one debt, idempotent per (obligation_id, action).

        `amount` of None means "read the outstanding balance from the store" -- that
        is how `execute()` calls it, since a scheduled action carries an id, not a
        number, and the balance may have moved since the decision was made.

        THE ORDER RULE. We never call `order.create`. The merchant's own order id
        travels with the link in `notes.order_id` and as the leading segment of
        `reference_id`, so the settlement webhook is attributable back to the
        merchant's order and their backend keeps its own key.

        DEVIATION, stated plainly: the Create Payment Link API has no `order_id`
        parameter, and it rejects unknown keys with a 400 ("extra fields sent"). A
        Standard Payment Link therefore mints its own internal order no matter what
        we do -- the only way to literally collect against the merchant's existing
        order is hosted Checkout with that `order_id`, which is not
        `payment_link.create()`. What we can guarantee, and do, is the part that
        actually protects the merchant: we never create a competing order, and the
        merchant's order id is carried on every link and every callback.
        """
        now = self._now()
        row = self._obligation_row(obligation_id)
        order_id = order_id_of(obligation_id)
        ref = reference_id_for(obligation_id, action)

        if amount is None:
            amount = int(row.get("amount_due") or 0) - int(row.get("amount_settled") or 0)
        contact = contact or row.get("contact")
        email = email or row.get("email")

        # -- idempotency, before anything leaves the process -------------------
        if self.con is not None:
            existing = store.live_payment_link(self.con, obligation_id, action, now)
            if existing:
                log.info("payment link reused for %s/%s: %s",
                         obligation_id, action, existing.get("short_url"))
                return {"ok": True, "reused": True, "link_id": existing.get("link_id"),
                        "short_url": existing.get("short_url"),
                        "reference_id": existing.get("reference_id"),
                        "order_id": existing.get("order_id"), "payload": None,
                        "mode": "reused",
                        "detail": f"existing link, expires {existing.get('expires_at')}"}
        else:
            log.warning("no store connection: payment link idempotency is DISABLED for %s",
                        obligation_id)

        if amount <= 0:
            # Nothing outstanding. Refusing here is a safety check, not an error path.
            log.info("payment link skipped for %s: nothing outstanding", obligation_id)
            return {"ok": False, "reused": False, "link_id": None, "short_url": None,
                    "reference_id": ref, "order_id": order_id, "payload": None,
                    "mode": "skipped", "detail": "NOTHING_OUTSTANDING"}

        expires = self._expire_by(now, row.get("opened_at"), window_ends_at)
        payload = self._build_payload(
            obligation_id, amount, ref, order_id, action, expires,
            contact=contact, email=email, name=name, description=description)

        # -- three ways out: no key, dry run, live -----------------------------
        if self.client is None:
            log.warning(
                "RAZORPAY CREDENTIALS ABSENT -- returning a STUB link, nothing was sent. "
                "obligation=%s action=%s amount=%s. Set RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET "
                "to go live.", obligation_id, action, amount)
            return self._record(obligation_id, action, payload, expires, now,
                                link_id=None, short_url=_stub_url(ref), status="stub",
                                mode="stub_no_key", dry_run=True,
                                detail="no credentials: stub link, nothing sent")

        if self.dry_run:
            log.info("DRY_RUN payment link for %s/%s -- payload NOT sent:\n%s",
                     obligation_id, action, _pretty(payload))
            return self._record(obligation_id, action, payload, expires, now,
                                link_id=None, short_url=_stub_url(ref), status="dry_run",
                                mode="dry_run", dry_run=True,
                                detail="DRY_RUN: payload built and logged, not sent")

        try:
            link = self.client.payment_link.create(payload)
        except Exception as e:                       # noqa: BLE001
            log.error("payment link creation failed for %s/%s: %s: %s",
                      obligation_id, action, type(e).__name__, e)
            return {"ok": False, "reused": False, "link_id": None, "short_url": None,
                    "reference_id": ref, "order_id": order_id, "payload": payload,
                    "mode": "live", "detail": f"{type(e).__name__}: {e}"}

        log.info("payment link created for %s/%s: %s",
                 obligation_id, action, link.get("short_url"))
        return self._record(obligation_id, action, payload, expires, now,
                            link_id=link.get("id"), short_url=link.get("short_url"),
                            status=str(link.get("status") or "created"),
                            mode="live", dry_run=False,
                            detail=f"payment_link {link.get('id')}")

    def _build_payload(self, obligation_id: str, amount: int, ref: str,
                       order_id: str | None, action: str, expires: datetime, *,
                       contact: str | None, email: str | None, name: str | None,
                       description: str | None) -> dict:
        customer: dict[str, str] = {}
        if name:
            customer["name"] = str(name)[:255]
        if email:
            customer["email"] = str(email)
        if contact:
            c = str(contact).strip()
            # Razorpay requires 8-14 characters including the country code. A number
            # outside that is a data problem; dropping the field beats a 400 that
            # loses the whole link.
            if 8 <= len(c) <= 14:
                customer["contact"] = c
            else:
                log.warning("contact %r for %s is not 8-14 chars -- omitted from payload",
                            c, obligation_id)

        payload: dict = {
            "amount": int(amount),
            "currency": "INR",
            "description": (description
                            or f"Complete your pending payment to {self.merchant_name}")[:2048],
            "reference_id": ref,
            # expire_by is Unix epoch SECONDS. An ISO string is rejected.
            "expire_by": int(expires.timestamp()),
            # We send the message ourselves (Task 2) so the copy, the language and the
            # contact budget stay under the gates. Letting Razorpay notify would put
            # contacts outside G4/G5 and outside quiet hours.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {
                "obligation_id": obligation_id,
                # The merchant's own key. Empty string, never a fabricated id, when the
                # debt is an invoice or a subscription rather than an order.
                "order_id": order_id or "",
                "recovery_action": action,
                "source": "razorrecovery",
            },
        }
        if customer:
            payload["customer"] = customer
        return payload

    def _record(self, obligation_id: str, action: str, payload: dict, expires: datetime,
                now: datetime, *, link_id: str | None, short_url: str | None, status: str,
                mode: str, dry_run: bool, detail: str) -> dict:
        if self.con is not None:
            try:
                store.save_payment_link(
                    self.con, obligation_id, action, link_id=link_id, short_url=short_url,
                    reference_id=payload["reference_id"],
                    order_id=payload["notes"]["order_id"] or None,
                    amount=payload["amount"], status=status,
                    expires_at=expires.isoformat(), created_at=now.isoformat(),
                    dry_run=dry_run)
            except Exception as e:                   # noqa: BLE001
                # A store failure must not un-send a link that Razorpay already made.
                log.error("payment link created but NOT recorded for %s/%s: %s",
                          obligation_id, action, e)
        return {"ok": True, "reused": False, "link_id": link_id, "short_url": short_url,
                "reference_id": payload["reference_id"],
                "order_id": payload["notes"]["order_id"] or None,
                "payload": payload, "mode": mode, "detail": detail}

    def execute(self, action_type: str, obligation_id: str, idem_key: str) -> ExecResult:
        self.calls.append(idem_key)

        if action_type in LINK_ACTIONS:
            # Note this runs in DRY_RUN too: create_payment_link handles the mode
            # itself, so a dry run exercises the real payload builder instead of a
            # branch that skips it. A dry run that skips the code under test proves
            # nothing.
            r = self.create_payment_link(obligation_id, action=action_type)
            if not r["ok"]:
                return ExecResult(ok=False, detail=r["detail"])
            return ExecResult(ok=True, detail=f"{r['detail']} {r['short_url'] or ''}".strip())

        if self.dry_run:
            return ExecResult(ok=True, detail=f"DRY_RUN: would {action_type} on {obligation_id}")

        if action_type == "RETRY":
            # See the class docstring. We record the intent; we do not debit.
            return ExecResult(ok=False, detail="INTENT_ONLY: server-initiated debit not enabled")

        return ExecResult(ok=False, detail=f"{action_type} not executable via API")


def _stub_url(reference_id: str) -> str:
    """A link-shaped string that goes nowhere. Obviously fake on sight, in a log."""
    return f"https://rzp.invalid/dry/{hashlib.sha1(reference_id.encode()).hexdigest()[:12]}"


def _pretty(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def build_executor(con=None, *, dry_run: bool | None = None, clock=None,
                   window_hours: int = 168, merchant_name: str | None = None):
    """The env-reading factory. Degrades to a stub, loudly, and never raises.

    Three independent things can be missing -- the SDK, the credentials, the intent
    to go live -- and none of them may take the app down. DRY_RUN defaults to true,
    so the only way a customer hears from us is if someone sets DRY_RUN=false and
    supplies real keys.
    """
    if dry_run is None:
        dry_run = _env_flag("DRY_RUN", True)
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    merchant_name = merchant_name or os.getenv("MERCHANT_NAME") or "the merchant"

    client = None
    if not key_id or not key_secret:
        log.warning("RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set -- Razorpay executor "
                    "runs in STUB mode. Links are fake, nothing is sent.")
    else:
        try:
            import razorpay                          # noqa: PLC0415
            client = razorpay.Client(auth=(key_id, key_secret))
        except Exception as e:                       # noqa: BLE001
            log.warning("razorpay SDK unavailable (%s: %s) -- STUB mode. Links are fake.",
                        type(e).__name__, e)

    if not dry_run and client is None:
        log.warning("DRY_RUN=false but there is no Razorpay client. Nothing will be sent. "
                    "This is a stub, not a live run.")
    log.info("RazorpayExecutor: dry_run=%s client=%s", dry_run, "live" if client else "stub")
    return RazorpayExecutor(client, con=con, dry_run=dry_run, clock=clock,
                            window_hours=window_hours, merchant_name=merchant_name)
