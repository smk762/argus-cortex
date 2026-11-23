from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

import argus_cortex
from argus_cortex import utc_now_iso
from argus_cortex.timestamps import TimeSpec


def test_default_is_utc_to_the_second() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    stamp = utc_now_iso()
    after = datetime.now(UTC)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", stamp)
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)
    assert before <= parsed <= after


@pytest.mark.parametrize(
    ("timespec", "pattern"),
    [
        ("minutes", r"T\d\d:\d\d\+00:00"),
        ("milliseconds", r"T\d\d:\d\d:\d\d\.\d{3}\+00:00"),
        ("microseconds", r"T\d\d:\d\d:\d\d\.\d{6}\+00:00"),
        ("auto", r"T\d\d:\d\d:\d\d(\.\d{6})?\+00:00"),
    ],
)
def test_timespec_is_isoformats(timespec: TimeSpec, pattern: str) -> None:
    assert re.search(pattern + "$", utc_now_iso(timespec))


def test_an_unknown_timespec_is_refused() -> None:
    with pytest.raises(ValueError):
        utc_now_iso("fortnights")  # type: ignore[arg-type]


def test_exported_at_the_top_level() -> None:
    assert "utc_now_iso" in argus_cortex.__all__
