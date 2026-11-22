"""Wall-clock timestamps the stages record (a job's start and end, a row's ``created_at``): one spelling of "now"."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

TimeSpec = Literal["auto", "hours", "minutes", "seconds", "milliseconds", "microseconds"]


def utc_now_iso(timespec: TimeSpec = "seconds") -> str:
    """The current UTC time as ISO 8601 with a ``+00:00`` offset, e.g. ``2026-11-23T06:44:00+00:00``.

    *timespec* is :meth:`datetime.isoformat`'s: ``"auto"`` keeps the microseconds a bare ``isoformat()`` writes
    (and drops them when they happen to be zero), for a record that orders or times events under a second.
    """
    return datetime.now(UTC).isoformat(timespec=timespec)
