"""
The Ops API. Everything an operator does that is not a decision.

The route list is short on purpose: stop, start, change the mode, change the
window, look at what needs a human, and read back who changed what. Anything
that mutates goes through `app/services/ops.py` so it lands in the audit table --
there is no path here that changes operator state without leaving a row.
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.api import dashboard
from app.config_loader import load_config
from app.services import ops
from app.workers.reconciler import reconcile

router = APIRouter(prefix="/api/ops", tags=["ops"])


def con():
    return dashboard.con()


@router.get("/state")
def state():
    """One call, everything the Ops tab renders. Polled, so it stays cheap."""
    c = con()
    merchant = ops.default_merchant()
    cfg = load_config()
    ps = ops.pause_set(c)
    return {
        "merchant_id": merchant,
        "paused": ps.active,
        "paused_global": ps.is_global,
        "pauses": ps.as_json(),
        "mode": ops.effective_dry_run(c),
        "live_readiness": live_readiness(),
        "quiet_hours": ops.quiet_hours(c, merchant),
        "quiet_hours_all": ops.quiet_hours_all(c),
        "config": {
            "base_version": cfg.version,
            "effective_version": ops.config_for(c, merchant).version,
            "path": "config/default.yaml",
            "max_contacts_7d": cfg.max_contacts_7d,
            "max_rung": cfg.max_rung,
            "window_hours": cfg.window_hours,
            "max_attempts": cfg.max_attempts,
        },
        "today": ops.today(c),
        "attention": ops.attention(c)["counts"],
        "audit": ops.audit(c, 12),
        "ingest_note": ("a pause stops deciding and executing. webhooks, settlement "
                        "matching and the abandonment sweep keep running, because a "
                        "paused engine that stops listening comes back to a ledger "
                        "that drifted while it was off"),
    }


def live_readiness() -> dict:
    """What going live would and would not actually do.

    The confirm step should show facts, not a warning. With no Razorpay keys and
    no SMTP host, flipping to live changes almost nothing -- and an operator who
    is told "you are now live" when nothing can be sent has been misled.
    """
    smtp = bool(os.getenv("SMTP_HOST")) and bool(
        os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"))
    rzp = bool(os.getenv("RAZORPAY_KEY_ID")) and bool(os.getenv("RAZORPAY_KEY_SECRET"))
    gaps = []
    # An em dash, not the `--` this codebase writes in comments: these two strings
    # are the only ones in this module that a person reads on screen rather than in
    # a source file, and `--` mid-sentence in a UI looks like a typo.
    if not rzp:
        gaps.append("no RAZORPAY_KEY_ID/SECRET — payment links stay stubs")
    if not smtp:
        gaps.append("no SMTP_HOST/SMTP_FROM — emails are counted, not sent")
    return {"razorpay_credentials": rzp, "smtp_configured": smtp,
            "will_actually_send": smtp, "gaps": gaps,
            "summary": ("real messages will leave this process" if smtp
                        else "nothing can leave this process yet")}


@router.post("/pause")
def pause(scope: str = "global", value: str | None = None, reason: str = "",
          who: str = "operator"):
    """Stop deciding and acting. Cancels what was pending. Keeps ingesting."""
    try:
        return ops.pause(con(), scope, value, reason=reason, who=who)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/resume")
def resume(pause_id: int | None = None, who: str = "operator"):
    return ops.resume(con(), pause_id, who=who)


@router.post("/mode")
def mode(dry_run: bool | None = None, confirm: bool = False, who: str = "operator",
         note: str | None = None, clear: bool = False):
    """Dry run <-> live.

    Going live requires `confirm=true`. The guard is server-side rather than a
    browser dialog: a confirm that only exists in the UI is not a control, and
    this endpoint is reachable with curl.
    """
    c = con()
    if clear:
        return ops.set_dry_run(c, None, who=who, note=note or "cleared, follows env")
    if dry_run is None:
        raise HTTPException(400, "pass dry_run=true|false, or clear=true")
    if dry_run is False and not confirm:
        raise HTTPException(400, {
            "error": "going live needs confirm=true",
            "what_this_changes": live_readiness(),
            "current": ops.effective_dry_run(c)})
    return ops.set_dry_run(c, bool(dry_run), who=who, note=note)


@router.get("/attention")
def attention():
    """The things that do not resolve themselves."""
    return ops.attention(con())


@router.post("/reconcile")
def reconcile_now():
    """Ask the source of truth about every UNKNOWN action. The Reconcile button."""
    from app.services.executor import build_executor        # noqa: PLC0415

    c = con()
    out = reconcile(c, build_executor(con=c))
    out["remaining_unknown"] = int(c.execute(
        "SELECT COUNT(*) FROM actions WHERE status = 'UNKNOWN'").fetchone()[0])
    return out


@router.post("/quiet_hours")
def quiet_hours(start: int, end: int, merchant_id: str | None = None,
                who: str = "operator", note: str | None = None):
    """Set this merchant's contact window.

    G12 is not weakened and `config/default.yaml` is not edited. The override
    lives in the database and the live worker builds a config from it, which is
    why the effective config version changes when you do this.
    """
    try:
        return ops.set_quiet_hours(con(), merchant_id or ops.default_merchant(),
                                   start, end, who=who, note=note)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/quiet_hours/reset")
def quiet_hours_reset(merchant_id: str | None = None, who: str = "operator"):
    """Drop the override and go back to config/default.yaml."""
    c = con()
    m = merchant_id or ops.default_merchant()
    all_ = dict(ops.quiet_hours_all(c))
    all_.pop(m, None)
    ops.set_setting(c, ops.K_QUIET, all_, who=who, note=f"reset {m} to file default")
    return ops.quiet_hours(c, m)


@router.get("/audit")
def audit(limit: int = 50):
    """Who changed what, when, and what it was before."""
    return {"changes": ops.audit(con(), limit),
            "note": "a config version says settings changed; this says what they were"}


@router.get("/today")
def today():
    return ops.today(con(), datetime.now())
