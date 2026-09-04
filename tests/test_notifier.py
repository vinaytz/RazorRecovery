"""
The notifier: value tiers, the PII boundary, and graceful degradation.

`test_no_pii_in_prompt` is the one that matters. Everything else is cost control;
that one is the promise that a customer's name never reaches a third party.
"""
from __future__ import annotations

import smtplib
from datetime import datetime

import pytest

from app.domain.models import ActionType, FailureClass
from app.repos import store
from app.services import notifier as N
from app.services.llm import (
    DiskCache,
    GeminiLLM,
    StubLLM,
    message_cache_key,
    recovery_email_prompt,
)

# Realistic PII. Every one of these must be absent from the prompt.
NAME = "Asha Iyer"
EMAIL = "asha.iyer@example.com"
PHONE = "+919876543210"
CUSTOMER_ID = "cust_HgT91xKp2"
OBLIGATION_ID = "order_PtVQ8xK2mNr4Lz"
LINK = "https://rzp.io/i/plink_abc123"


class SpyLLM:
    """Records the exact prompt string. The PII test asserts on what it captured."""

    mode = "spy"

    def __init__(self):
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    def classify_error(self, text):
        return FailureClass.UNKNOWN

    def write_recovery_email(self, **kw):
        self.kwargs.append(kw)
        self.prompts.append(recovery_email_prompt(
            kw["failure_class"], kw["amount_band"], kw["action"],
            kw["merchant"], kw["language"], kw["fresh"]))
        return {"subject": "Payment of {{AMOUNT}} to {{MERCHANT}} is pending",
                "body": "Hello {{NAME}}, your {{AMOUNT}} payment to {{MERCHANT}} "
                        "did not go through. Complete it here: {{LINK}}",
                "cached": False}


# -- THE PII BOUNDARY ------------------------------------------------------

def test_no_pii_in_prompt():
    """The model is given a class, a band, an action, a merchant, a language.

    It is not given a name, an email, a phone number, a customer id, an
    obligation id, or the payment link. `compose` cannot leak them: it never
    receives them in the first place.
    """
    spy = SpyLLM()
    N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 250_000,
              merchant="ExampleMart", llm=spy)

    assert len(spy.prompts) == 1
    prompt = spy.prompts[0]
    for secret in (NAME, EMAIL, PHONE, CUSTOMER_ID, OBLIGATION_ID, LINK,
                   "Asha", "iyer", "9876543210", "2500", "250000"):
        assert secret.lower() not in prompt.lower(), f"{secret!r} leaked into the prompt"

    # And the kwargs themselves carry only the five permitted fields.
    assert set(spy.kwargs[0]) == {"failure_class", "amount_band", "action",
                                 "merchant", "language", "fresh"}
    assert spy.kwargs[0]["amount_band"] == "L"       # a band, never the amount


def test_compose_signature_cannot_accept_pii():
    """A regression guard: if someone adds a `name=` parameter, this fails."""
    import inspect
    params = set(inspect.signature(N.compose).parameters)
    for forbidden in ("name", "email", "contact", "phone", "customer_id",
                      "obligation_id", "link", "to"):
        assert forbidden not in params


def test_pii_enters_only_at_render():
    spy = SpyLLM()
    msg = N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 250_000,
                    merchant="ExampleMart", llm=spy)
    assert NAME not in msg.body and LINK not in msg.body   # placeholders only
    subject, body = N.render(msg, name=NAME, amount=250_000, merchant="ExampleMart",
                             link=LINK, action=ActionType.PAY_LINK)
    assert NAME in body and LINK in body                   # hydrated locally
    assert "{{" not in body and "{{" not in subject


def test_render_survives_a_missing_name():
    msg = N.static_message(FailureClass.INSUFFICIENT_FUNDS, ActionType.REMIND)
    _, body = N.render(msg, name=None, amount=10_000, merchant="ExampleMart",
                       link=LINK, action=ActionType.REMIND)
    assert "Hello there," in body and "{{" not in body


@pytest.mark.parametrize("action", [ActionType.REMIND, ActionType.PAY_LINK,
                                    ActionType.METHOD_CHANGE])
def test_every_contact_action_carries_the_link(action):
    """A reminder with no way to pay is a worse reminder."""
    msg = N.static_message(FailureClass.CARD_EXPIRED, action)
    _, body = N.render(msg, name=NAME, amount=250_000, merchant="ExampleMart",
                       link=LINK, action=action)
    assert LINK in body


def test_no_link_drops_the_whole_block_not_just_the_slot():
    """Stripping the slot alone leaves "complete it here:" pointing at nothing."""
    msg = N.static_message(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK)
    _, body = N.render(msg, name=NAME, amount=250_000, merchant="ExampleMart",
                       link=None, action=ActionType.PAY_LINK)
    assert "{{" not in body
    assert "here:" not in body            # no dangling lead-in
    assert body.rstrip().endswith("Reply STOP to opt out of these reminders.")


def test_a_model_that_emits_a_url_is_sanitised():
    class Rogue(SpyLLM):
        def write_recovery_email(self, **kw):
            return {"subject": "Pay now",
                    "body": "Hello {{NAME}}, pay {{AMOUNT}} to {{MERCHANT}} at "
                            "https://evil.example/steal or call +919999999999 "
                            "or mail us at scam@evil.example"}

    msg = N.compose(FailureClass.UNKNOWN, ActionType.PAY_LINK, 250_000, llm=Rogue())
    # The only URL a customer may receive is the one WE minted.
    assert "evil.example" not in msg.body
    assert "919999999999" not in msg.body
    _, body = N.render(msg, name=NAME, amount=250_000, merchant="M", link=LINK,
                       action=ActionType.PAY_LINK)
    assert LINK in body


# -- VALUE TIERS -----------------------------------------------------------

@pytest.mark.parametrize("amount,tier", [
    (1, "static"), (49_999, "static"),
    (50_000, "cached_llm"), (999_999, "cached_llm"),
    (1_000_000, "fresh_llm"), (5_000_000, "fresh_llm"),
])
def test_tier_boundaries(amount, tier):
    assert N.choose_tier(amount) == tier


def test_small_amount_never_calls_the_model():
    spy = SpyLLM()
    msg = N.compose(FailureClass.INSUFFICIENT_FUNDS, ActionType.REMIND, 30_000, llm=spy)
    assert spy.prompts == []                  # not one call. that is the point.
    assert msg.tier == "static" and msg.used_llm is False


def test_mid_tier_uses_the_model_and_a_cache_key_of_three_fields():
    spy = SpyLLM()
    N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 250_000, llm=spy)
    assert spy.kwargs[0]["fresh"] is False    # cacheable
    key = message_cache_key("CARD_EXPIRED", "PAY_LINK", "en")
    assert key == "CARD_EXPIRED|PAY_LINK|en"  # no amount, no customer


def test_high_tier_is_generated_fresh():
    spy = SpyLLM()
    N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 2_500_000, llm=spy)
    assert spy.kwargs[0]["fresh"] is True     # worth paying per case


def test_cache_serves_a_repeat_without_a_second_call(tmp_path):
    """Two cases, same (class, action, language), one generation."""
    cache = DiskCache(tmp_path / "c")
    calls = {"n": 0}

    class FakeModel:
        def generate_content(self, prompt):
            calls["n"] += 1
            return type("R", (), {"text": '{"subject":"S {{AMOUNT}}",'
                                          '"body":"Hi {{NAME}} pay {{AMOUNT}} to '
                                          '{{MERCHANT}} {{LINK}}"}'})()

    llm = GeminiLLM(cache=cache)
    llm._model = FakeModel()

    kw = dict(failure_class="CARD_EXPIRED", amount_band="L", action="PAY_LINK",
              merchant="ExampleMart", language="en", fresh=False)
    first = llm.write_recovery_email(**kw)
    second = llm.write_recovery_email(**kw)

    assert calls["n"] == 1                    # one model call for two cases
    assert first["cached"] is False and second["cached"] is True
    assert first["body"] == second["body"]
    assert llm.email_cache_hits == 1


def test_fresh_tier_bypasses_the_cache(tmp_path):
    cache = DiskCache(tmp_path / "c")
    calls = {"n": 0}

    class FakeModel:
        def generate_content(self, prompt):
            calls["n"] += 1
            return type("R", (), {"text": '{"subject":"S","body":"Hi {{NAME}} {{AMOUNT}} '
                                          '{{MERCHANT}}"}'})()

    llm = GeminiLLM(cache=cache)
    llm._model = FakeModel()
    kw = dict(failure_class="CARD_EXPIRED", amount_band="XL", action="PAY_LINK",
              merchant="M", language="en", fresh=True)
    llm.write_recovery_email(**kw)
    llm.write_recovery_email(**kw)
    assert calls["n"] == 2                    # high value: no reuse


def test_stub_llm_writes_copy_with_no_key():
    out = StubLLM().write_recovery_email(
        failure_class="CARD_EXPIRED", amount_band="L", action="PAY_LINK",
        merchant="ExampleMart", language="en", fresh=False)
    assert "{{NAME}}" in out["body"] and "{{AMOUNT}}" in out["body"]
    assert out["source"] == "stub"


def test_a_dead_model_falls_back_to_the_static_template():
    class Broken:
        mode = "broken"
        def classify_error(self, t): return FailureClass.UNKNOWN
        def write_recovery_email(self, **kw): raise RuntimeError("quota exhausted")

    msg = N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 250_000, llm=Broken())
    assert msg.tier == "cached_llm->static" and msg.used_llm is False
    assert "{{NAME}}" in msg.body             # still a usable message


def test_no_llm_at_a_high_tier_says_so_in_the_tier():
    msg = N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 2_500_000, llm=None)
    assert msg.tier == "fresh_llm->static"    # visible downgrade, not a silent one


# -- COPY QUALITY ----------------------------------------------------------
# Small things, but they are the whole email a customer actually sees. Copy that
# reads as machine-assembled undoes the point of paying for a model at all.

@pytest.mark.parametrize("action", list(N.SUBJECT))
@pytest.mark.parametrize("amount", [40_000, 300_000, 1_250_000])
def test_the_footer_appears_exactly_once(action, amount):
    """Every tier, every contact action. The footer owns the already-paid
    acknowledgement and the opt-out line, so the template must not carry its own."""
    msg = N.compose(FailureClass.INSUFFICIENT_FUNDS, action, amount, llm=StubLLM())
    _, body = N.render(msg, name=NAME, amount=amount, merchant="ExampleMart",
                       link="https://rzp.io/l/abc", action=action)
    assert body.lower().count("already paid") == 1
    assert body.lower().count("reply stop") == 1


def test_a_model_that_writes_its_own_footer_does_not_get_it_twice():
    """The prompt says not to. This is what happens when it does anyway."""
    class Rogue:
        mode = "rogue"
        def classify_error(self, t): return FailureClass.UNKNOWN
        def write_recovery_email(self, **kw):
            return {"subject": "Payment issue",
                    "body": ("Hello {{NAME}},\n\nYour {{AMOUNT}} payment to "
                             "{{MERCHANT}} failed. Pay here: {{LINK}}\n\nIf you have "
                             "already paid, please ignore this.\n"
                             "Reply STOP to unsubscribe.")}

    msg = N.compose(FailureClass.CARD_EXPIRED, ActionType.PAY_LINK, 2_000_000, llm=Rogue())
    assert msg.body.lower().count("already paid") == 1
    assert msg.body.lower().count("reply stop") == 1
    assert "unsubscribe" not in msg.body.lower()      # ours, not theirs
    assert "\n\n\n" not in msg.body                   # no hole where it was


def test_the_subject_matches_what_we_are_asking_for():
    """A PAY_LINK titled "is pending" asks nothing. The stub returns an empty
    subject on purpose so the per-action line is used."""
    for action, expected in [(ActionType.PAY_LINK, "Complete your"),
                             (ActionType.REMIND, "is pending"),
                             (ActionType.METHOD_CHANGE, "another payment method")]:
        msg = N.compose(FailureClass.INSUFFICIENT_FUNDS, action, 1_250_000, llm=StubLLM())
        subject, _ = N.render(msg, name=NAME, amount=1_250_000, merchant="ExampleMart",
                              link="https://rzp.io/l/abc", action=action)
        assert expected in subject, f"{action.value}: {subject!r}"


def test_an_unclassified_failure_says_nothing_rather_than_unknown():
    """"(unknown)" is not a reason a customer can act on."""
    msg = N.compose(FailureClass.UNKNOWN, ActionType.PAY_LINK, 1_250_000, llm=StubLLM())
    assert "unknown" not in msg.body.lower()
    assert "did not go through." in msg.body


def test_customer_copy_is_written_in_the_second_person():
    """ACTION_ASK describes the customer TO the model, so it says "they". Anything
    written FOR the customer has to say "you"."""
    for action in (ActionType.REMIND, ActionType.PAY_LINK, ActionType.METHOD_CHANGE):
        msg = N.compose(FailureClass.INSUFFICIENT_FUNDS, action, 300_000, llm=StubLLM())
        _, body = N.render(msg, name=NAME, amount=300_000, merchant="ExampleMart",
                           link="https://rzp.io/l/abc", action=action)
        for third in (" they ", " their ", " them "):
            assert third not in body.lower(), f"{action.value}: {body!r}"


# -- SENDING ---------------------------------------------------------------

def test_no_smtp_host_falls_back_to_counter(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    n = N.build_notifier()
    assert isinstance(n, N.CounterNotifier)
    r = n.send(to=EMAIL, subject="s", body="b")
    assert r.ok and n.count == 1              # counted, not sent


def test_smtp_host_without_a_from_address_falls_back(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    assert isinstance(N.build_notifier(), N.CounterNotifier)


def test_email_notifier_sends_via_smtplib(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"], captured["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): captured["tls"] = True
        def login(self, u, p): captured["login"] = u
        def send_message(self, msg): captured["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setenv("SMTP_FROM", "recovery@example.com")

    n = N.build_notifier()
    assert isinstance(n, N.EmailNotifier)
    r = n.send(to=EMAIL, subject="Complete your payment",
               body="Hello Asha", meta={"obligation_id": OBLIGATION_ID})

    assert r.ok and n.count == 1
    assert captured["host"] == "smtp.example.com" and captured["tls"] is True
    assert captured["msg"]["To"] == EMAIL
    # The obligation id on the header is what makes an inbound reply attributable.
    assert captured["msg"]["X-RazorRecovery-Obligation"] == OBLIGATION_ID


def test_smtp_failure_is_a_result_not_a_crash(monkeypatch):
    class Dead:
        def __init__(self, *a, **k): raise OSError("connection refused")
    monkeypatch.setattr(smtplib, "SMTP", Dead)
    n = N.EmailNotifier(N.SmtpConfig(host="h", sender="s@example.com"))
    r = n.send(to=EMAIL, subject="s", body="b")
    assert r.ok is False and "SMTP_FAILED" in r.detail and n.failures == 1


def test_no_address_is_a_data_gap_not_a_delivery_failure():
    n = N.EmailNotifier(N.SmtpConfig(host="h", sender="s@example.com"))
    r = n.send(to=None, subject="s", body="b")
    assert r.ok is False and r.detail == "NO_EMAIL_ON_FILE"


def test_logs_mask_the_address():
    assert N._mask("asha.iyer@example.com") == "as***@example.com"


# -- the contact ledger is a constraint, not a log -------------------------

@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init(c)
    return c


def test_a_failed_send_does_not_spend_the_contact_cap(con):
    now = datetime(2026, 3, 10, 12, 0, 0)
    for ok in (True, False, False):
        store.record_contact(con, customer_id="cust1", obligation_id="ob1", case_id="c1",
                             channel="email", action="PAY_LINK", tier="static",
                             used_llm=False, subject="s", sent_at=now.isoformat(),
                             ok=ok, detail="d")
    # Three rows, one contact. An SMTP outage must not silence us for a week.
    assert con.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 3
    assert store.contacts_last_7d(con, "cust1", now) == 1


def test_contacts_outside_the_window_do_not_count(con):
    now = datetime(2026, 3, 10, 12, 0, 0)
    store.record_contact(con, customer_id="cust1", obligation_id="ob1", case_id=None,
                         channel="email", action="REMIND", tier="static", used_llm=False,
                         subject="s", sent_at=datetime(2026, 2, 1).isoformat(),
                         ok=True, detail="d")
    assert store.contacts_last_7d(con, "cust1", now) == 0
