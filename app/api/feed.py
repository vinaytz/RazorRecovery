"""
The feed route. One GET, polled once a second by the Demo tab.

It is separate from `dashboard.py` because it is a different question: the
dashboard answers "what did the benchmark find", and this answers "what is
happening right now". They share a connection and nothing else.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import dashboard
from app.services import feed as feed_svc
from app.workers.live import time_scale

router = APIRouter(prefix="/api", tags=["feed"])


@router.get("/feed")
def live_feed(limit: int = feed_svc.DEFAULT_LIMIT,
              minutes: int = feed_svc.DEFAULT_MINUTES):
    """Everything that happened, newest first.

    `minutes` bounds the window rather than paginating it: this is a feed, not
    an archive, and the archive is the Cases and Decision tabs.
    """
    con = dashboard.con()
    out = feed_svc.feed(con, limit=max(1, min(int(limit), 200)),
                        minutes=max(1, min(int(minutes), 1440)))
    out["time_scale"] = time_scale()
    return out
