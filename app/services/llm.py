"""
LLM job ①: free-text error string -> FailureClass.

The whole design rule in one line: **the LLM never returns a money action.** It
returns a member of a fixed enum. Anything it says that is not in that enum
becomes `UNKNOWN`, which is a value the gates and ladder already handle. So the
worst a hallucinating or hostile model can do is make us classify a failure as
unknown -- it cannot make us charge anyone.

Why an LLM at all, when `ingest.ERROR_REASON_MAP` already covers the codes:
Razorpay's `error_reason` is a tidy enum, but `error_description` is free text
that varies by issuer and gateway, and the long tail is where a code-only
classifier silently degrades to UNKNOWN. The map runs first and the model only
sees what the map missed.

Cache: `sha1(text)` on disk. ~40 distinct strings across 5,000 cases, so ~40
calls, not 5,000. The cache is also what makes `LLM_MODE=gemini` reproducible
enough to demo twice.

`StubLLM` is the default and is written first. A dead API key must never block
the benchmark. `LLM_MODE=stub` is not a degraded mode -- it is a keyword
classifier over the same interface, and the benchmark output is byte-identical
with it wired in, which is the proof that the engine does not depend on a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Protocol

from app.domain.models import FailureClass

CACHE_DIR = Path(__file__).resolve().parents[2] / ".llm_cache"

# Keyword -> FailureClass, longest-phrase-first so "insufficient balance" wins
# over a bare "balance". Ordered, not a dict lookup: real issuer strings are
# prose, not tokens.
KEYWORDS: tuple[tuple[str, FailureClass], ...] = (
    ("insufficient balance", FailureClass.INSUFFICIENT_FUNDS),
    ("insufficient funds", FailureClass.INSUFFICIENT_FUNDS),
    ("not enough balance", FailureClass.INSUFFICIENT_FUNDS),
    ("exceeds available", FailureClass.INSUFFICIENT_FUNDS),
    ("limit exceeded", FailureClass.INSUFFICIENT_FUNDS),
    ("card has expired", FailureClass.CARD_EXPIRED),
    ("card expired", FailureClass.CARD_EXPIRED),
    ("expired card", FailureClass.CARD_EXPIRED),
    ("expiry", FailureClass.CARD_EXPIRED),
    ("card is blocked", FailureClass.CARD_BLOCKED),
    ("card blocked", FailureClass.CARD_BLOCKED),
    ("card disabled", FailureClass.CARD_BLOCKED),
    ("blocked for online", FailureClass.CARD_BLOCKED),
    ("restricted card", FailureClass.CARD_BLOCKED),
    ("issuer down", FailureClass.ISSUER_DOWN),
    ("bank is down", FailureClass.ISSUER_DOWN),
    ("bank down", FailureClass.ISSUER_DOWN),
    ("issuer unavailable", FailureClass.ISSUER_DOWN),
    ("technical error", FailureClass.ISSUER_DOWN),
    ("try again later", FailureClass.ISSUER_DOWN),
    ("gateway timeout", FailureClass.NETWORK_ERROR),
    ("network error", FailureClass.NETWORK_ERROR),
    ("connection", FailureClass.NETWORK_ERROR),
    ("timed out", FailureClass.NETWORK_ERROR),
    ("mandate", FailureClass.MANDATE_INVALID),
    ("e-mandate", FailureClass.MANDATE_INVALID),
    ("standing instruction", FailureClass.MANDATE_INVALID),
    ("autopay", FailureClass.MANDATE_INVALID),
    ("otp", FailureClass.AUTH_ABANDONED),
    ("3ds", FailureClass.AUTH_ABANDONED),
    ("authentication", FailureClass.AUTH_ABANDONED),
    ("cancelled by user", FailureClass.AUTH_ABANDONED),
    ("customer cancelled", FailureClass.AUTH_ABANDONED),
    ("collect request expired", FailureClass.AUTH_ABANDONED),
    ("did not complete", FailureClass.CHECKOUT_ABANDONED),
    ("abandoned", FailureClass.CHECKOUT_ABANDONED),
    ("closed the checkout", FailureClass.CHECKOUT_ABANDONED),
)

PROMPT = """Classify this payment failure message into exactly one category.

Categories: INSUFFICIENT_FUNDS, CARD_EXPIRED, CARD_BLOCKED, ISSUER_DOWN,
NETWORK_ERROR, AUTH_ABANDONED, MANDATE_INVALID, CHECKOUT_ABANDONED, UNKNOWN

Rules:
- Reply with JSON only: {"failure_class": "<CATEGORY>"}
- If the message does not clearly match a category, use UNKNOWN.
- Never invent a category that is not in the list.

Message: """


class LLM(Protocol):
    """The port. Both implementations return an enum member, never text."""

    def classify_error(self, text: str) -> FailureClass: ...


class DiskCache:
    """sha1(text) -> classification. Survives restarts, so a demo rerun is free."""

    def __init__(self, path: Path = CACHE_DIR):
        self.path = path
        self.hits = 0
        self.misses = 0

    def _file(self, text: str) -> Path:
        return self.path / f"{hashlib.sha1(text.encode()).hexdigest()}.json"

    # Both accessors swallow everything. A cache is an optimisation; a broken one
    # must degrade to "no cache", never to a failed classification. OSError is not
    # enough -- a bad path raises ValueError, a corrupt file raises JSONDecodeError,
    # and on a read-only mount you get neither reliably.
    def get(self, text: str) -> str | None:
        try:
            f = self._file(text)
            if not f.exists():
                self.misses += 1
                return None
            self.hits += 1
            return json.loads(f.read_text()).get("failure_class")
        except Exception:                            # noqa: BLE001
            return None

    def put(self, text: str, value: str) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self._file(text).write_text(json.dumps({"failure_class": value}))
        except Exception:                            # noqa: BLE001
            pass

    @property
    def stats(self) -> dict:
        try:
            entries = len(list(self.path.glob("*.json"))) if self.path.exists() else 0
        except Exception:                            # noqa: BLE001
            entries = 0
        return {"hits": self.hits, "misses": self.misses, "entries": entries}


def _coerce(raw: str | None) -> FailureClass:
    """Model output -> enum. Anything unrecognised is UNKNOWN, never an exception."""
    if not raw:
        return FailureClass.UNKNOWN
    token = re.sub(r"[^A-Z_]", "", str(raw).strip().upper())
    try:
        return FailureClass(token)
    except ValueError:
        return FailureClass.UNKNOWN


class StubLLM:
    """Deterministic keyword classifier. The default, and written first.

    No network, no key, no variance. Same input, same output, forever -- which is
    why the benchmark can be wired to it and still reproduce byte-for-byte.
    """

    mode = "stub"

    def __init__(self, cache: DiskCache | None = None):
        self.cache = cache or DiskCache()
        self.calls = 0

    def classify_error(self, text: str) -> FailureClass:
        if not text or not text.strip():
            return FailureClass.UNKNOWN
        cached = self.cache.get(text)
        if cached:
            return _coerce(cached)

        self.calls += 1
        low = text.strip().lower()
        result = FailureClass.UNKNOWN
        for needle, fc in KEYWORDS:
            if needle in low:
                result = fc
                break
        self.cache.put(text, result.value)
        return result


class GeminiLLM:
    """Gemini Flash, JSON mode, temperature 0. Same interface, same guarantees.

    Every failure path returns `UNKNOWN` rather than raising: no API key, quota
    exhausted, malformed JSON, a category the model invented. A recovery engine
    that stops deciding because a classifier is down is worse than one that
    classifies a few failures as unknown.
    """

    mode = "gemini"
    MODEL = "gemini-1.5-flash"

    def __init__(self, cache: DiskCache | None = None, api_key: str | None = None,
                 fallback: LLM | None = None):
        self.cache = cache or DiskCache()
        self.calls = 0
        self.errors = 0
        self.fallback = fallback or StubLLM(self.cache)
        self._model = None
        key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
        if key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=key)
                self._model = genai.GenerativeModel(
                    self.MODEL,
                    generation_config={"temperature": 0,
                                      "response_mime_type": "application/json"})
            except Exception:                        # noqa: BLE001
                self._model = None

    @property
    def live(self) -> bool:
        return self._model is not None

    def classify_error(self, text: str) -> FailureClass:
        if not text or not text.strip():
            return FailureClass.UNKNOWN
        cached = self.cache.get(text)
        if cached:
            return _coerce(cached)
        if self._model is None:
            # No key configured. Keyword classifier, and the caller can see
            # `live is False` rather than being told a model ran.
            return self.fallback.classify_error(text)

        self.calls += 1
        try:
            resp = self._model.generate_content(PROMPT + text)
            data = json.loads(resp.text)
            result = _coerce(data.get("failure_class"))
        except Exception:                            # noqa: BLE001
            self.errors += 1
            return self.fallback.classify_error(text)

        self.cache.put(text, result.value)
        return result


def get_llm(mode: str | None = None) -> LLM:
    """`LLM_MODE=stub|gemini`. Stub is the default: an absent key is not an outage."""
    m = (mode or os.environ.get("LLM_MODE", "stub")).strip().lower()
    if m == "gemini":
        return GeminiLLM()
    return StubLLM()
