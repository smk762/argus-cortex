from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from argus_cortex.consent import (
    CONSENT_REFUSALS,
    CONSENT_USES,
    ConsentRecord,
    ConsentVerdict,
    consent_matches,
    consent_schema,
    consent_valid,
)
from argus_cortex.provenance import Provenance

RELEASE = "a" * 64
IMG = "b" * 64
CLIP = "c" * 64
UTC = UTC


def record(**over: Any) -> ConsentRecord:
    data: dict[str, Any] = {
        "release_sha256": RELEASE,
        "subject": "Jo Example",
        "collected_by": "studio-a",
        "signed_on": date(2026, 1, 10),
        "expires_on": date(2027, 1, 10),
        "age_check": {"checked_by": "studio-a", "checked_on": date(2026, 1, 10)},
        "face": True,
        "voice": True,
        "deliveries": ["neutral", "warm"],
        "uses": ["avatar", "speech"],
        "reference_images": [IMG],
        "reference_recordings": [CLIP],
        "revocable": True,
        "notice_days": 30,
        "on_revoke": "delete",
    }
    data.update(over)
    return ConsentRecord.model_validate(data)


AT = datetime(2026, 6, 1, 12, tzinfo=UTC)


def check(rec: ConsentRecord, **over: Any) -> ConsentVerdict:
    kw: dict[str, Any] = {"use": "avatar", "likeness": "face", "at": AT}
    kw.update(over)
    return consent_valid(rec, **kw)


# --- the record -------------------------------------------------------------------------------------


def test_a_complete_record_round_trips() -> None:
    rec = record()
    data = json.loads(rec.model_dump_json())
    assert data["uses"] == ["avatar", "speech"]  # literal order, stable
    assert ConsentRecord.model_validate(data) == rec


def test_unknown_fields_are_refused_and_the_record_is_frozen() -> None:
    with pytest.raises(ValidationError, match="id_scan"):
        record(id_scan="…")
    with pytest.raises(ValidationError):
        record().face = False


def test_expiry_must_be_stated_even_when_none() -> None:
    data = json.loads(record().model_dump_json())
    del data["expires_on"]
    with pytest.raises(ValidationError, match="expires_on"):
        ConsentRecord.model_validate(data)
    assert record(expires_on=None).expires_on is None


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"face": False, "voice": False, "deliveries": [], "reference_recordings": []}, "face, the voice or both"),
        ({"deliveries": []}, "deliveries"),
        ({"voice": False}, "deliveries are for a voice"),
        ({"reference_images": []}, "reference images"),
        ({"reference_recordings": []}, "reference recordings"),
        ({"uses": []}, "uses"),
        ({"uses": ["avatar", "broadcast"]}, "uses"),
        ({"subject": "  "}, "subject"),
        ({"collected_by": ""}, "collected_by"),
        ({"notice_days": -1}, "notice_days"),
        ({"on_revoke": "archive"}, "on_revoke"),
        ({"release_sha256": "A" * 64}, "release_sha256"),
        ({"reference_images": ["nope"]}, "reference_images"),
        ({"expires_on": date(2026, 1, 10)}, "expires after"),
        ({"age_check": None}, "age_check"),
        ({"age_check": {"checked_by": "", "checked_on": date(2026, 1, 10)}}, "checked_by"),
        (
            {"revocation": {"requested_on": date(2026, 5, 2), "effective_on": date(2026, 5, 1), "by": "Jo"}},
            "on or after",
        ),
        ({"age_check": {"checked_by": "  ", "checked_on": date(2026, 1, 10)}}, "names who did it"),
        ({"age_check": {"checked_by": "studio-a", "checked_on": date(2026, 1, 11)}}, "at or before signing"),
        ({"deliveries": ["neutral", "  "]}, "names each delivery"),
        (
            {"revocation": {"requested_on": date(2026, 1, 9), "effective_on": date(2026, 2, 1), "by": "Jo"}},
            "day the release was signed",
        ),
    ],
)
def test_an_incomplete_record_is_refused(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        record(**over)


def test_a_record_cannot_be_changed_in_place() -> None:
    rec = record()
    with pytest.raises(AttributeError):
        rec.reference_images.clear()  # type: ignore[attr-defined]
    hash(rec)


def test_face_only_and_voice_only_releases() -> None:
    face = record(voice=False, deliveries=[], reference_recordings=[])
    assert check(face).ok
    assert check(face, likeness="voice", use="speech", delivery="neutral").reason == "likeness-not-covered"
    voice = record(face=False, reference_images=[])
    assert check(voice, likeness="voice", use="speech", delivery="warm").ok
    assert check(voice).reason == "likeness-not-covered"


# --- consent_valid --------------------------------------------------------------------------------


def test_a_covered_use_is_ok() -> None:
    verdict = check(record())
    assert verdict.ok and verdict.reason is None and verdict.detail


def test_training_is_off_unless_listed() -> None:
    assert check(record(), use="training").reason == "use-not-covered"
    assert check(record(uses=["training"]), use="training").ok


def test_revoked_from_the_effective_day() -> None:
    rec = record(revocation={"requested_on": date(2026, 5, 1), "effective_on": date(2026, 6, 1), "by": "Jo"})
    assert check(rec, at=datetime(2026, 5, 31, 23, 59, tzinfo=UTC)).ok
    assert check(rec, at=datetime(2026, 6, 1, tzinfo=UTC)).reason == "revoked"


def test_dates_bound_the_release() -> None:
    rec = record()
    assert check(rec, at=datetime(2026, 1, 9, 23, 59, tzinfo=UTC)).reason == "not-yet-signed"
    assert check(rec, at=datetime(2026, 1, 10, tzinfo=UTC)).ok
    assert check(rec, at=datetime(2027, 1, 9, 23, 59, tzinfo=UTC)).ok
    assert check(rec, at=datetime(2027, 1, 10, tzinfo=UTC)).reason == "expired"
    assert check(record(expires_on=None), at=datetime(2099, 1, 1, tzinfo=UTC)).ok


def test_days_are_taken_in_utc_so_one_instant_gets_one_verdict() -> None:
    rec = record()
    # 2027-01-10 00:30 in UTC+2 is still 2027-01-09 in UTC: not yet expired.
    plus_two = datetime(2027, 1, 10, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert check(rec, at=plus_two).ok
    assert check(rec, at=plus_two.astimezone(UTC)).ok


def test_the_first_failure_wins() -> None:
    # Revoked, expired, out of scope and the wrong likeness at once: revocation is reported.
    rec = record(
        face=False,
        reference_images=[],
        revocation={"requested_on": date(2026, 2, 1), "effective_on": date(2026, 2, 1), "by": "Jo"},
    )
    at = datetime(2028, 1, 1, tzinfo=UTC)
    assert check(rec, at=at, use="training").reason == "revoked"
    assert check(record(face=False, reference_images=[]), at=at, use="training").reason == "expired"
    assert check(record(face=False, reference_images=[]), use="training").reason == "likeness-not-covered"
    assert check(record(), use="training").reason == "use-not-covered"
    voice = {"likeness": "voice", "delivery": "singing"}
    assert check(record(), use="training", **voice).reason == "use-not-covered"
    assert check(record(), use="speech", **voice).reason == "delivery-not-covered"


def test_an_unknown_use_is_refused_not_raised() -> None:
    assert check(record(), use="hologram").reason == "use-not-covered"


def test_caller_bugs_raise() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        check(record(), at=datetime(2026, 6, 1))
    with pytest.raises(ValueError, match="likeness"):
        check(record(), likeness="gait")
    with pytest.raises(ValueError, match="delivery"):
        check(record(), likeness="voice", use="speech")


def test_verdict_is_ok_or_has_a_reason() -> None:
    with pytest.raises(ValidationError):
        ConsentVerdict(ok=True, reason="revoked")
    with pytest.raises(ValidationError):
        ConsentVerdict(ok=False)
    with pytest.raises(ValidationError):
        ConsentVerdict(ok=False, reason="no-record")  # mask's own reason, not cortex's


# --- consent_matches ------------------------------------------------------------------------------


def consented(**over: Any) -> Provenance:
    data: dict[str, Any] = {
        "kind": "consented",
        "source": "studio session",
        "license": "likeness release",
        "subject": "Jo Example",
        "release_sha256": RELEASE,
    }
    data.update(over)
    return Provenance.model_validate(data)


def test_consent_matches_the_release_and_subject_it_names() -> None:
    rec = record()
    assert consent_matches(rec, consented())
    assert not consent_matches(rec, consented(subject="Someone Else"))
    assert not consent_matches(rec, consented(release_sha256="d" * 64))
    assert not consent_matches(rec, Provenance(kind="synthetic", source="x", license="CC0-1.0"))


# --- schema ---------------------------------------------------------------------------------------

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "consent-schema.json"


def test_committed_schema_matches_consent() -> None:
    assert SCHEMA_FILE.read_text() == consent_schema(), "consent-schema.json is stale; run `make schema`"


def test_schema_lists_every_use_and_refusal() -> None:
    defs = json.loads(consent_schema())["$defs"]
    assert defs["ConsentRecord"]["properties"]["uses"]["items"]["enum"] == list(CONSENT_USES)
    reason = defs["ConsentVerdict"]["properties"]["reason"]["anyOf"][0]
    assert reason["enum"] == list(CONSENT_REFUSALS)
    assert "expires_on" in defs["ConsentRecord"]["required"]
