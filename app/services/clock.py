"""Clock port. Real time in production, virtual time in the benchmark.

This is one of only two swap points between the live system and the simulation.
The engine never calls either -- `now` always arrives on the snapshot.
"""
from __future__ import annotations

from datetime import datetime, timedelta


class RealClock:
    def now(self) -> datetime:
        return datetime.now()

    def advance(self, **kw) -> None:
        raise RuntimeError("cannot advance the real clock")


class VirtualClock:
    """Lets a 7-day recovery window run in a couple of seconds."""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kw) -> datetime:
        self._now = self._now + timedelta(**kw)
        return self._now

    def set(self, t: datetime) -> None:
        self._now = t
