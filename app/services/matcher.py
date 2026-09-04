"""
Settlement matching: deciding which debt a payment belongs to, and saying how
sure we are.

This is the file that decides whether the headline number is trustworthy.

Money arrives with whatever identifiers the payment happened to carry. Sometimes
that is the merchant's order id and there is nothing to work out. Sometimes it is
a bank transfer with a reference nobody typed correctly, and the honest answer is
"an amount close to this one arrived in the right week and it is probably that
debt". Both get counted -- but they must never be counted the same way.

THE LADDER, strongest first:

  1 EXACT_ID              the payment carries an id we already track: the
                          obligation id in our own payment link's notes, our
                          reference_id, or an order / invoice / subscription id
                          that is the obligation itself.        certain
  2 CUSTOMER_ID           Razorpay's customer_id matches exactly, and resolves
                          to one open debt.                     certain
  3 CONTACT_AMOUNT_WINDOW the payer's phone or email matches, the amount is
                          within 2%, and it is inside the recovery window.
                                                                strong
  4 AMOUNT_WINDOW         only the amount (within 2%) and the window match.
                          THIS IS A HEURISTIC AND IS LABELLED AS ONE.  heuristic
  5 LEDGER_HOOK           a human told us, via POST /api/cases/{id}/settled.
                          Used for cash and bank transfer, where no webhook
                          exists to observe.                    asserted

Two rules:

  A HEURISTIC MATCH IS NEVER PRESENTED AS CERTAIN. Level 4 carries
  `confidence="heuristic"` all the way to the dashboard, and the dashboard shows
  the split rather than a single total.

  AN AMBIGUOUS HEURISTIC MATCHES NOTHING. If two open debts are both within 2% of
  the amount in the same window, we cannot tell which one paid, so we close
  neither and put the settlement on the operator's attention list. Closing the
  wrong case would record a recovery that did not happen and keep chasing the
  customer who did pay.

Why level 1 needs our link's notes at all: the Create Payment Link API has no
`order_id` parameter, so a payment made through our link comes back carrying an
order id Razorpay minted for the link -- an id the merchant has never seen. We
carry the real obligation id in `notes.obligation_id` and in the leading segment
of `reference_id`, and this is where that pays off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.executor import obligation_from_reference_id

log = logging.getLogger("razorrecovery.matcher")

AMOUNT_TOLERANCE = 0.02        # +/- 2%, per the ladder
DEFAULT_WINDOW_HOURS = 168     # 7 days, the recovery window

# level -> (basis, confidence, one-line explanation for the UI)
LADDER: dict[int, tuple[str, str, str]] = {
    1: ("EXACT_ID", "certain",
        "the payment carries an id we already track"),
    2: ("CUSTOMER_ID", "certain",
        "Razorpay customer_id matched exactly and resolved to one open debt"),
    3: ("CONTACT_AMOUNT_WINDOW", "strong",
        "phone or email matched, amount within 2%, inside the window"),
    4: ("AMOUNT_WINDOW", "heuristic",
        "only the amount (within 2%) and the window matched -- a guess, not a fact"),
    5: ("LEDGER_HOOK", "asserted",
        "a human recorded this settlement; we did not observe the money"),
}

CERTAIN_LEVELS = frozenset({1, 2})


@dataclass(frozen=True)
class Match:
    """The result of matching. `obligation_id is None` means we could not tell."""
    level: int | None
    basis: str
    confidence: str
    obligation_id: str | None
    evidence: str
    candidates: int = 1

    @property
    def certain(self) -> bool:
        return self.level in CERTAIN_LEVELS

    @property
    def matched(self) -> bool:
        return self.obligation_id is not None

    def as_dict(self) -> dict:
        return {"level": self.level, "basis": self.basis, "confidence": self.confidence,
                "obligation_id": self.obligation_id, "evidence": self.evidence,
                "candidates": self.candidates, "certain": self.certain}


NO_MATCH = Match(level=None, basis="UNMATCHED", confidence="none",
                 obligation_id=None,
                 evidence="no id, customer, contact or amount matched an open debt",
                 candidates=0)


def _level(n: int, obligation_id: str | None, evidence: str,
           candidates: int = 1) -> Match:
    basis, confidence, _ = LADDER[n]
    return Match(level=n, basis=basis, confidence=confidence,
                 obligation_id=obligation_id, evidence=evidence,
                 candidates=candidates)


# -- the payload's identifiers --------------------------------------------

def candidate_ids(payload: dict) -> list[str]:
    """Every id in the payload that could name an obligation, strongest first.

    Our own markers come first deliberately: if a payment arrived through a link
    we minted, `notes.obligation_id` is the truth and the entity's own order_id is
    Razorpay's bookkeeping for the link, not the merchant's order.
    """
    pay = _payment_of(payload)
    link = _link_of(payload)
    out: list[str] = []

    for src in (pay, link):
        notes = src.get("notes")
        if isinstance(notes, dict):
            for key in ("obligation_id", "order_id"):
                v = notes.get(key)
                if v:
                    out.append(str(v))
        ref = src.get("reference_id")
        if ref:
            got = obligation_from_reference_id(str(ref))
            if got:
                out.append(got)

    for key in ("subscription_id", "invoice_id", "order_id", "id"):
        v = pay.get(key)
        if v:
            out.append(str(v))

    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def payment_id_of(payload: dict) -> str:
    """The idempotency key for a settlement. One payment, one recovery."""
    pay = _payment_of(payload)
    for key in ("id", "payment_id"):
        v = pay.get(key)
        if v:
            return str(v)
    # No payment id: fall back to something stable rather than something random,
    # so a redelivery still collapses instead of double-counting.
    return f"syn:{payload.get('event')}:{payload.get('created_at')}"


def amount_of_settlement(payload: dict) -> int:
    pay = _payment_of(payload)
    for key in ("amount", "amount_paid"):
        v = pay.get(key)
        if isinstance(v, int) and v > 0:
            return v
    return 0


# -- the ladder ------------------------------------------------------------

def match_settlement(con, payload: dict, now: datetime,
                     window_hours: int = DEFAULT_WINDOW_HOURS) -> Match:
    """Walk the ladder, strongest rung first. Stop at the first rung that fires."""
    pay = _payment_of(payload)
    amount = amount_of_settlement(payload)
    since = (now - timedelta(hours=window_hours)).isoformat()

    # 1. an id we already track.
    for oid in candidate_ids(payload):
        row = con.execute("SELECT id FROM obligations WHERE id = ?", (oid,)).fetchone()
        if row:
            return _level(1, oid, f"obligation id {oid} appeared in the payment")

    # 2. Razorpay customer_id, resolving to exactly one open debt.
    cust = pay.get("customer_id")
    if cust:
        rows = _open_obligations(con, since, customer_id=str(cust))
        if len(rows) == 1:
            return _level(2, rows[0]["id"],
                          f"customer_id {cust} has exactly one open debt")
        if len(rows) > 1 and amount:
            near = [r for r in rows if _within(r["amount_due"], amount)]
            if len(near) == 1:
                return _level(2, near[0]["id"],
                              f"customer_id {cust} had {len(rows)} open debts; "
                              f"the amount picked one")

    # 3. phone or email + amount within 2% + window.
    if amount:
        for field, value in (("contact", pay.get("contact")), ("email", pay.get("email"))):
            if not value:
                continue
            rows = [r for r in _open_obligations(con, since, **{field: str(value)})
                    if _within(r["amount_due"], amount)]
            if len(rows) == 1:
                return _level(3, rows[0]["id"],
                              f"{field} matched and the amount is within 2%")
            if len(rows) > 1:
                # Same payer, two debts of nearly the same size. Oldest first is a
                # defensible convention and it is stated, not hidden.
                rows.sort(key=lambda r: r["opened_at"])
                return _level(3, rows[0]["id"],
                              f"{field} matched {len(rows)} debts of a similar amount; "
                              f"took the oldest", candidates=len(rows))

    # 4. amount + window only. HEURISTIC.
    if amount:
        rows = [r for r in _open_obligations(con, since)
                if _within(r["amount_due"], amount)]
        if len(rows) == 1:
            return _level(4, rows[0]["id"],
                          "amount within 2% inside the window, nothing else matched")
        if len(rows) > 1:
            # We genuinely cannot tell. Matching anyway would close the wrong case
            # AND keep chasing the customer who actually paid.
            log.warning("settlement of %s matched %d open debts on amount alone -- "
                        "not closing any of them", amount, len(rows))
            return Match(level=4, basis=LADDER[4][0], confidence="ambiguous",
                         obligation_id=None,
                         evidence=(f"{len(rows)} open debts are within 2% of this "
                                   f"amount in the window -- cannot tell which paid"),
                         candidates=len(rows))

    return NO_MATCH


def ledger_match(case_id: str, obligation_id: str, who: str | None,
                 reference: str | None) -> Match:
    """Level 5. A human asserts the money arrived out of band.

    Deliberately not called 'certain'. We did not see this money; somebody told us
    about it, and that is a different kind of true.
    """
    detail = f"recorded by {who or 'an operator'}"
    if reference:
        detail += f", reference {reference}"
    return _level(5, obligation_id, f"{detail} against case {case_id}")


def via_our_link(payload: dict) -> bool:
    """Did this money arrive through a link WE minted?

    `notes.source == "razorrecovery"` is set by the executor's payload builder and
    is the one marker in the payment that only we could have put there. It is what
    separates "we nudged them and they paid" from "they paid anyway".
    """
    for src in (_payment_of(payload), _link_of(payload)):
        notes = src.get("notes")
        if isinstance(notes, dict) and notes.get("source") == "razorrecovery":
            return True
    return False


# -- helpers ---------------------------------------------------------------

def _within(due: int | None, paid: int) -> bool:
    """Within 2%. A partial payment of half the debt is not this debt settling."""
    if not due or not paid:
        return False
    return abs(int(due) - int(paid)) <= max(1, int(round(int(due) * AMOUNT_TOLERANCE)))


def _open_obligations(con, since: str, **equals) -> list[dict]:
    q = ("SELECT id, customer_id, amount_due, opened_at FROM obligations "
         "WHERE status = 'OPEN' AND opened_at >= ?")
    params: list = [since]
    for col, val in equals.items():
        q += f" AND {col} = ?"
        params.append(val)
    q += " ORDER BY opened_at"
    return [dict(r) for r in con.execute(q, params)]


def _entities(payload: dict) -> dict:
    p = payload.get("payload")
    return p if isinstance(p, dict) else {}


def _payment_of(payload: dict) -> dict:
    e = (_entities(payload).get("payment") or {}).get("entity")
    if isinstance(e, dict):
        return e
    for name in ("payment_link", "subscription", "invoice", "order"):
        e = (_entities(payload).get(name) or {}).get("entity")
        if isinstance(e, dict):
            return e
    return {}


def _link_of(payload: dict) -> dict:
    e = (_entities(payload).get("payment_link") or {}).get("entity")
    return e if isinstance(e, dict) else {}
