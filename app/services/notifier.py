"""
Notifier port. The thing that actually reaches a human.

Two implementations behind one interface, chosen by whether SMTP credentials
exist. `CounterNotifier` is the benchmark half: it counts what would have been
sent and nothing leaves the process, which is what lets the four-arm simulation
run 2,000 cases without mailing anybody. `EmailNotifier` is the live half.

TWO RULES GOVERN THIS FILE.

1. VALUE-TIERED GENERATION. Writing copy with an LLM costs money and latency per
   call, and most failed payments are small. So the spend follows the value:

     amount < 50,000 paise (Rs 500)      static template. NO LLM CALL AT ALL.
     amount < 10,00,000 paise (Rs 10k)   LLM, cached on
                                         (failure_class, action, language)
     amount >= 10,00,000 paise (Rs 10k)  LLM fresh per case, merchant tone,
                                         full context

   The middle tier is where the engineering is. There are 9 failure classes x 3
   contact actions x a handful of languages -- on the order of 40 live
   combinations -- so a cache keyed on those three fields serves thousands of
   cases from tens of generations. The wording of "your card expired, here is a
   link" does not need to be re-invented per customer; the wording of a
   Rs 40,000 recovery does.

2. PII NEVER LEAVES THE PROCESS. `llm.write_recovery_email()` receives a failure
   class, an amount BAND (not the amount), an action, a merchant name and a
   language. It never receives a name, a phone number, an email address, a
   customer id, an obligation id, or a payment link. It returns copy containing
   `{{NAME}}` / `{{AMOUNT}}` / `{{MERCHANT}}` / `{{LINK}}` placeholders, and
   those are filled in locally, after the response has come back.
   `tests/test_notifier.py::test_no_pii_in_prompt` pins this.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

from app.domain.models import ActionType, FailureClass, amount_band, rupees

log = logging.getLogger("razorrecovery.notifier")

# The tier boundaries, in paise. Named because these are a cost decision and
# somebody will want to move them without reading the module docstring.
STATIC_BELOW = 50_000        # Rs 500     -- template only, no model
CACHED_BELOW = 1_000_000     # Rs 10,000  -- model, cached on the segment
# above CACHED_BELOW           -- model, fresh per case

# Placeholders the model is allowed to emit. Anything else it invents is left as
# literal text rather than being resolved, so a hallucinated `{{CARD_NUMBER}}`
# can never be filled from our data.
SLOTS = ("NAME", "AMOUNT", "MERCHANT", "LINK", "ACTION")

# What a contact action is asking the customer to DO. Used by both the static
# templates and the LLM prompt, so the two tiers stay consistent in meaning.
ASK = {
    ActionType.REMIND: "pay using the same method they already tried",
    ActionType.PAY_LINK: "pay via a secure link",
    ActionType.METHOD_CHANGE: "pay using a different card or UPI",
}

# Tier 1. Static, DLT-shaped, no model involved. One per (failure_class, action)
# would be 27 templates; the failure class only changes the first sentence, so
# the templates compose instead.
WHY = {
    FailureClass.INSUFFICIENT_FUNDS: "your payment did not go through due to insufficient balance",
    FailureClass.CARD_EXPIRED: "your card appears to have expired",
    FailureClass.CARD_BLOCKED: "your bank declined the card for online use",
    FailureClass.ISSUER_DOWN: "your bank was temporarily unavailable",
    FailureClass.NETWORK_ERROR: "the payment could not be completed due to a network issue",
    FailureClass.AUTH_ABANDONED: "the payment was not authenticated",
    FailureClass.MANDATE_INVALID: "your autopay instruction is no longer active",
    FailureClass.CHECKOUT_ABANDONED: "your order was not completed at checkout",
    FailureClass.UNKNOWN: "your payment did not go through",
}

DO = {
    ActionType.REMIND: "You can complete it whenever convenient:",
    ActionType.PAY_LINK: "You can complete it securely here:",
    ActionType.METHOD_CHANGE: "You can complete it with another card or UPI here:",
}

SUBJECT = {
    ActionType.REMIND: "Your {{MERCHANT}} payment of {{AMOUNT}} is pending",
    ActionType.PAY_LINK: "Complete your {{MERCHANT}} payment of {{AMOUNT}}",
    ActionType.METHOD_CHANGE: "Try another payment method for {{MERCHANT}}",
}

FOOTER = ("\n\nIf you have already paid, please ignore this message -- "
          "it may have crossed with your payment.\n"
          "Reply STOP to opt out of these reminders.")


@dataclass
class Message:
    """What we are about to send. `tier` and `used_llm` are for the audit trail."""
    subject: str
    body: str
    tier: str
    used_llm: bool
    cached: bool = False


@dataclass
class SendResult:
    ok: bool
    detail: str = ""
    channel: str = "email"
    message: Message | None = None


# -- tier 1: static --------------------------------------------------------

def static_message(failure_class: FailureClass, action: ActionType) -> Message:
    """No model, no network, no cost. Deterministic for a given (class, action).

    Every contact action gets the link, REMIND included. A reminder that tells a
    customer their payment failed and gives them no way to complete it is a worse
    reminder, and the executor mints a link for all three actions anyway -- an
    unreachable link is an obligation somebody could pay without us matching it.
    The link sits on its own line so `render` can drop the whole block when there
    is nothing to link to.
    """
    why = WHY.get(failure_class, WHY[FailureClass.UNKNOWN])
    do = DO.get(action, DO[ActionType.REMIND])
    body = (f"Hello {{{{NAME}}}},\n\n"
            f"We noticed {why} for your {{{{AMOUNT}}}} payment to {{{{MERCHANT}}}}.\n\n"
            f"{do}\n{{{{LINK}}}}{FOOTER}")
    return Message(subject=SUBJECT.get(action, SUBJECT[ActionType.REMIND]),
                   body=body, tier="static", used_llm=False)


# -- tiers 2 and 3: the model ----------------------------------------------

def choose_tier(amount: int) -> str:
    if amount < STATIC_BELOW:
        return "static"
    if amount < CACHED_BELOW:
        return "cached_llm"
    return "fresh_llm"


def compose(failure_class: FailureClass, action: ActionType, amount: int, *,
            merchant: str = "the merchant", language: str = "en",
            llm=None) -> Message:
    """Pick a tier by value and produce placeholder copy. NO PII passes through.

    Note what this function does NOT take: no name, no email, no contact, no
    obligation id, no link. It cannot leak them because it never receives them.
    Hydration happens in `render`, after this returns.
    """
    tier = choose_tier(amount)
    if tier == "static" or llm is None:
        m = static_message(failure_class, action)
        if tier != "static":
            # A missing LLM must not silently downgrade a high-value contact
            # without saying so -- that is a quality regression an operator
            # should be able to see in the log.
            log.warning("tier %s wanted a model but none is configured -- "
                        "falling back to the static template (class=%s action=%s band=%s)",
                        tier, failure_class.value, action.value, amount_band(amount))
            m.tier = f"{tier}->static"
        return m

    band = amount_band(amount)
    try:
        out = llm.write_recovery_email(
            failure_class=failure_class.value,
            amount_band=band,                 # "S"/"M"/"L"/"XL", never the amount
            action=action.value,
            merchant=merchant,
            language=language,
            fresh=(tier == "fresh_llm"),
        )
    except Exception as e:                               # noqa: BLE001
        log.warning("message generation failed (%s: %s) -- static template",
                    type(e).__name__, e)
        m = static_message(failure_class, action)
        m.tier = f"{tier}->static"
        return m

    subject = _sanitise(out.get("subject") or SUBJECT.get(action, ""))
    body = _sanitise(out.get("body") or "")
    if not body:
        m = static_message(failure_class, action)
        m.tier = f"{tier}->static"
        return m
    body = _add_footer(body)
    return Message(subject=subject, body=body, tier=tier, used_llm=True,
                   cached=bool(out.get("cached")))


# The footer owns the already-paid acknowledgement and the opt-out line, and the
# prompt tells the model not to write either. A model that ignores that gets its
# version dropped rather than printed alongside ours -- an email that says "if you
# have already paid" twice reads like it was assembled by a machine, which is
# exactly the impression a recovery notice cannot afford.
_ALREADY_PAID = re.compile(
    r"[^.\n]*\b(?:if|should)\s+you(?:'ve| have)?\s+(?:already\s+)?"
    r"(?:paid|made|settled|completed)[^.\n]*\.?", re.I)
_OPT_OUT = re.compile(r"[^.\n]*\b(?:reply|text)\s+STOP\b[^.\n]*\.?", re.I)


def _add_footer(body: str) -> str:
    """Strip the model's own footer sentences, then append ours exactly once."""
    body = _OPT_OUT.sub("", _ALREADY_PAID.sub("", body))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()   # tidy the gaps left behind
    return body + FOOTER


# A model that returns a URL, a phone number or an email address is a model
# trying to put un-vetted contact detail in front of a customer. Strip it. The
# only link a customer may receive is the one WE minted, via {{LINK}}.
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _sanitise(text: str) -> str:
    text = _URL.sub("{{LINK}}", str(text))
    text = _EMAIL.sub("", text)
    text = _PHONE.sub("", text)
    return text.strip()


# -- hydration: where PII enters, locally ----------------------------------

def render(message: Message, *, name: str | None, amount: int, merchant: str,
           link: str | None, action: ActionType) -> tuple[str, str]:
    """Fill the slots. This is the ONLY place customer data touches the copy."""
    values = {
        "NAME": (name or "there").strip() or "there",
        "AMOUNT": rupees(amount),
        "MERCHANT": merchant,
        "LINK": link or "",
        "ACTION": ASK.get(action, ""),
    }
    def fill(s: str) -> str:
        if not values["LINK"]:
            s = _drop_link_block(s)
        for slot in SLOTS:
            s = s.replace(f"{{{{{slot}}}}}", values[slot])
        # An unfilled slot is a bug, but shipping the literal "{{FOO}}" to a
        # customer is worse than shipping nothing there.
        return re.sub(r"\{\{[A-Z_]+\}\}", "", s).strip()
    return fill(message.subject), fill(message.body)


def _drop_link_block(text: str) -> str:
    """No link: remove the {{LINK}} line and the sentence that introduced it.

    Stripping the slot alone would leave "You can complete it securely here:"
    pointing at nothing, which reads like a broken email. The lead-in is
    identifiable because it ends in a colon.
    """
    lines = text.split("\n")
    keep: list[str] = []
    for line in lines:
        if "{{LINK}}" in line and not line.replace("{{LINK}}", "").strip():
            if keep and keep[-1].rstrip().endswith(":"):
                keep.pop()
            continue
        keep.append(line)
    return "\n".join(keep)


# -- the two notifiers -----------------------------------------------------

class CounterNotifier:
    """Counts, sends nothing. The benchmark half of the port, and the fallback.

    Every field an EmailNotifier would use is recorded, so a dry run or a
    credential-less deploy still produces the full audit trail -- you can read
    exactly what would have gone out and to whom.
    """

    channel = "counter"

    def __init__(self):
        self.sent: list[dict] = []

    @property
    def count(self) -> int:
        return len(self.sent)

    def send(self, *, to: str | None, subject: str, body: str,
             meta: dict | None = None) -> SendResult:
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "meta": meta or {}})
        return SendResult(ok=True, detail=f"counted (nothing sent), total={self.count}",
                          channel=self.channel)


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    sender: str = "recovery@example.com"
    sender_name: str = "RazorRecovery"
    use_tls: bool = True
    timeout: int = 15


class EmailNotifier:
    """smtplib. STARTTLS by default, and a send failure is a result, not a raise.

    A dead mail server must not take the recovery loop down: the action records
    FAILED with the SMTP detail, the reconciler and the operator can see it, and
    the case stays open for the next rung. Crashing here would strand every
    other case in the batch.
    """

    channel = "email"

    def __init__(self, cfg: SmtpConfig):
        self.cfg = cfg
        self.sent: list[dict] = []
        self.failures = 0

    @property
    def count(self) -> int:
        return len(self.sent)

    def send(self, *, to: str | None, subject: str, body: str,
             meta: dict | None = None) -> SendResult:
        if not to:
            # No address is a data gap, not a delivery failure. Saying so keeps
            # "we could not reach them" separate from "the mail server broke".
            log.warning("no email address on %s -- nothing sent",
                        (meta or {}).get("obligation_id", "?"))
            return SendResult(ok=False, detail="NO_EMAIL_ON_FILE", channel=self.channel)

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((self.cfg.sender_name, self.cfg.sender))
        msg["To"] = to
        # The obligation id on the reply path is what makes an inbound reply
        # attributable back to a case (promise-to-pay, Batch 3).
        oid = (meta or {}).get("obligation_id")
        if oid:
            msg["X-RazorRecovery-Obligation"] = str(oid)
            msg["Reply-To"] = self.cfg.sender
        msg.set_content(body)

        try:
            with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout) as s:
                if self.cfg.use_tls:
                    s.starttls()
                if self.cfg.user:
                    s.login(self.cfg.user, self.cfg.password or "")
                s.send_message(msg)
        except Exception as e:                           # noqa: BLE001
            self.failures += 1
            log.error("SMTP send failed to %s: %s: %s", _mask(to), type(e).__name__, e)
            return SendResult(ok=False, detail=f"SMTP_FAILED: {type(e).__name__}: {e}",
                              channel=self.channel)

        self.sent.append({"to": to, "subject": subject, "meta": meta or {}})
        log.info("email sent to %s subject=%r", _mask(to), subject)
        return SendResult(ok=True, detail=f"sent to {_mask(to)}", channel=self.channel)


def _mask(addr: str) -> str:
    """Logs are read by more people than the mailbox is. Mask the local part."""
    if "@" not in addr:
        return "***"
    local, _, domain = addr.partition("@")
    return f"{local[:2]}***@{domain}"


def build_notifier():
    """SMTP_HOST/PORT/USER/PASS/FROM. Absent -> CounterNotifier, loudly.

    The fallback is deliberately the benchmark's notifier and not a no-op: the
    contact is still counted, still recorded, still visible on the dashboard.
    "We could not send" and "we decided not to send" must never look the same.
    """
    host = (os.getenv("SMTP_HOST") or "").strip()
    if not host:
        log.warning("SMTP_HOST not set -- using CounterNotifier. Contacts are COUNTED "
                    "and recorded but NO EMAIL IS SENT. Set SMTP_HOST/PORT/USER/PASS/FROM "
                    "to send for real.")
        return CounterNotifier()

    sender = (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "").strip()
    if not sender:
        log.warning("SMTP_HOST is set but SMTP_FROM/SMTP_USER is not -- using "
                    "CounterNotifier rather than sending from an unset address.")
        return CounterNotifier()

    try:
        port = int(os.getenv("SMTP_PORT") or 587)
    except ValueError:
        log.warning("SMTP_PORT is not a number -- defaulting to 587")
        port = 587

    cfg = SmtpConfig(
        host=host, port=port,
        user=(os.getenv("SMTP_USER") or "").strip() or None,
        password=os.getenv("SMTP_PASS") or None,
        sender=sender,
        sender_name=(os.getenv("SMTP_FROM_NAME") or "RazorRecovery").strip(),
        use_tls=(os.getenv("SMTP_TLS") or "true").strip().lower()
                not in ("0", "false", "no", "off"),
    )
    log.info("EmailNotifier live: %s:%s from=%s tls=%s",
             cfg.host, cfg.port, _mask(cfg.sender), cfg.use_tls)
    return EmailNotifier(cfg)
