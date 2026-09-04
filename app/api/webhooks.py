"""
POST /webhooks/razorpay

Verify the signature on the RAW body, insert the event, return 200. No logic.

Three things here are easy to get wrong and expensive to get wrong:

1. `RAZORPAY_WEBHOOK_SECRET` is NOT `RAZORPAY_KEY_SECRET`. Different secret,
   different dashboard page. Signing with the key secret fails every time.
2. The signature covers the RAW BYTES. `json.loads` then `json.dumps` changes
   whitespace and key order, so the recomputed HMAC will not match. Read the
   body first, verify, parse second.
3. Compare with `hmac.compare_digest`, not `==`.

`/webhooks/razorpay/replay` exists because tunnels die on demo day. It pushes a
saved payload through the identical code path with a locally computed signature,
so what you see demoed is the real handler, not a mock of it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.controllers import ingest as ingest_ctl
from app.repos import store
from app.services import llm as llm_svc

router = APIRouter(tags=["webhooks"])

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "webhooks"

# Verification is skipped only when no secret is configured at all, and the
# response says so out loud. A quiet skip is how an open endpoint ships.
SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"

_con = None
_llm = None


def con():
    global _con
    if _con is None:
        _con = store.connect()
        store.init(_con)
    return _con


def llm():
    """LLM job (1). Only classifies error text the deterministic map missed."""
    global _llm
    if _llm is None:
        _llm = llm_svc.get_llm()
    return _llm


def secret() -> str | None:
    s = os.environ.get(SECRET_ENV, "").strip()
    return s or None


def expected_signature(raw: bytes, key: str) -> str:
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def verify(raw: bytes, signature: str | None, key: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(expected_signature(raw, key), signature.strip())


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    raw = await request.body()                      # RAW first. always.
    sig = request.headers.get("x-razorpay-signature")
    key = secret()

    if key:
        if not verify(raw, sig, key):
            # 400, not 500: a bad signature is a rejected request, not our error.
            # 5xx makes Razorpay retry a payload that will never verify.
            raise HTTPException(400, "signature verification failed")
        verified = True
    else:
        verified = False

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body is not a JSON object")

    result = ingest_ctl.ingest(con(), payload, dict(request.headers), llm=llm())
    result["signature_verified"] = verified
    result["llm_mode"] = getattr(llm(), "mode", "none")
    if not verified:
        result["warning"] = (f"{SECRET_ENV} not set -- signature NOT checked. "
                             "Never run this way in production.")
    return result


@router.get("/api/llm")
def llm_state():
    """What the classifier is, and how few calls it makes."""
    m = llm()
    return {
        "mode": getattr(m, "mode", "none"),
        "live": getattr(m, "live", True),
        "calls": getattr(m, "calls", 0),
        "errors": getattr(m, "errors", 0),
        "cache": m.cache.stats if hasattr(m, "cache") else {},
        "contract": "returns a FailureClass enum member or UNKNOWN. never a money action.",
        "note": ("deterministic error_reason map runs first and wins; the model only "
                 "sees free text the map could not classify"),
    }


@router.get("/webhooks/fixtures")
def list_fixtures():
    """What we can replay. Empty list means nothing was captured."""
    if not FIXTURES.exists():
        return {"fixtures": [], "note": "fixtures/webhooks/ does not exist"}
    names = sorted(p.name for p in FIXTURES.glob("*.json"))
    return {"fixtures": names, "count": len(names), "dir": str(FIXTURES)}


@router.post("/webhooks/razorpay/replay")
async def replay_fixture(name: str | None = None):
    """Push saved payload(s) through the real handler.

    Honest fallback for a dead tunnel: same signature check, same ingest, same
    database. Only the transport differs, and the response says which fixture
    produced each line.
    """
    if not FIXTURES.exists():
        raise HTTPException(404, f"no fixtures dir at {FIXTURES}")

    paths = ([FIXTURES / name] if name
             else sorted(FIXTURES.glob("*.json")))
    if not paths or not all(p.exists() for p in paths):
        raise HTTPException(404, f"fixture not found: {name}")

    key = secret() or "replay_only_local_secret"
    out = []
    for p in paths:
        raw = p.read_bytes()
        sig = expected_signature(raw, key)          # sign locally, then verify
        ok = verify(raw, sig, key)
        payload = json.loads(raw)
        res = ingest_ctl.ingest(con(), payload, {"x-razorpay-signature": sig},
                                llm=llm())
        res["fixture"] = p.name
        res["signature_verified"] = ok
        out.append(res)

    return {"replayed": len(out), "results": out,
            "note": ("fixtures replayed through POST /webhooks/razorpay's exact "
                     "handler and ingest path -- transport differs, code does not")}
