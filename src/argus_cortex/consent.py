"""A real person's consent to the use of their face or voice, and the one check of it.

argus-mask#26 defined this (https://github.com/smk762/argus-mask/issues/26#issuecomment-6868745895).
Mask writes and revokes records; mask, curator and darkroom check them. The shape and the check live
here so every package refuses for the same reasons, in the same words.

- :class:`ConsentRecord`: one signed release. ``release_sha256`` is its key: a ``consented``
  :class:`~argus_cortex.provenance.Provenance` names a release, and the record for that release
  decides whether the source may be used. A re-signed release has a new hash, so a new record.
- :func:`consent_valid`: whether a record covers one use of one likeness at one moment. It returns a
  :class:`ConsentVerdict` whose ``reason`` is a closed :data:`ConsentRefusal` code.
- :func:`consent_matches`: whether a record is the one a ``Provenance`` names (same release, same
  subject). Separate, because curator checks a record with no ``Provenance`` to compare against.

A record names a person, so a service never serves one: endpoints return only a verdict. The record
holds hashes of the signed release and the reference captures, never the documents or any copy of
the subject's ID. The disclosure label is required on every output, so it is not a field.

The meaning of each field is argus-mask's (#26). Change this module only together with it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from argus_cortex.provenance import SHA256_PATTERN, Provenance
from argus_cortex.wire import render_schema

Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]

ConsentUse = Literal["avatar", "video", "speech", "training"]
"""``avatar``: a live face on a screen. ``video``: ``record`` output. ``speech``: audio only (``say``,
the Broadcast page). ``training``: fitting or fine-tuning a model on the captures. Adding a use is an
additive change."""

Likeness = Literal["face", "voice"]
OnRevoke = Literal["delete", "keep-labelled"]

ConsentRefusal = Literal[
    "revoked",
    "not-yet-signed",
    "expired",
    "likeness-not-covered",
    "use-not-covered",
    "delivery-not-covered",
]
"""Why :func:`consent_valid` refused, in the order it checks. Closed: every consumer gives these."""

CONSENT_USES: tuple[ConsentUse, ...] = get_args(ConsentUse)
LIKENESSES: tuple[Likeness, ...] = get_args(Likeness)
CONSENT_REFUSALS: tuple[ConsentRefusal, ...] = get_args(ConsentRefusal)


class AgeCheck(BaseModel):
    """The adult check against ID at signing: who did it and when. Never the ID or any copy of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_by: str = Field(..., min_length=1)
    checked_on: date


class Revocation(BaseModel):
    """Set by ``consent revoke``. Use stops on ``effective_on``; mask sets it to the day it runs.

    The release's notice period is the subject's commitment, not a grace period for the suite.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_on: date
    effective_on: date
    by: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Revocation:
        if not self.by.strip():
            raise ValueError("a revocation names who asked for it")
        if self.effective_on < self.requested_on:
            raise ValueError("a revocation takes effect on or after the day it was requested")
        return self


class ConsentRecord(BaseModel):
    """One signed likeness release, as the suite checks it. Keyed by ``release_sha256``.

    Free-text fields (``exclusions``, ``media``, ``territories``, ``notes``) are for people and are
    not checked by code. Payment and retention terms stay in the signed document.
    """

    # Frozen and strict, as Provenance is: the checks run only at construction, and a misspelt field
    # must not be dropped in silence.
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_sha256: Sha256
    subject: str = Field(..., min_length=1)
    collected_by: str = Field(..., min_length=1)
    signed_on: date
    expires_on: date | None = Field(..., description="None means no expiry; it must be stated, not omitted.")
    age_check: AgeCheck
    face: bool
    voice: bool
    deliveries: list[str] = Field(default_factory=list)
    uses: frozenset[ConsentUse] = Field(..., min_length=1)
    reference_images: list[Sha256] = Field(default_factory=list)
    reference_recordings: list[Sha256] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    media: list[str] = Field(default_factory=list)
    territories: list[str] = Field(default_factory=list)
    revocable: bool
    notice_days: int = Field(..., ge=0)
    on_revoke: OnRevoke
    revocation: Revocation | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _complete(self) -> ConsentRecord:
        if not self.subject.strip():
            raise ValueError("a consent record names its subject")
        if not self.collected_by.strip():
            raise ValueError("a consent record names who collected it")
        if not (self.face or self.voice):
            raise ValueError("a consent record opts in to the face, the voice or both")
        if self.voice and not [d for d in self.deliveries if d.strip()]:
            raise ValueError("a voice release lists the deliveries it covers")
        if not self.voice and self.deliveries:
            raise ValueError("deliveries are for a voice release; this one does not cover the voice")
        if self.face and not self.reference_images:
            raise ValueError("a face release binds its reference images by sha256")
        if self.voice and not self.reference_recordings:
            raise ValueError("a voice release binds its reference recordings by sha256")
        if self.expires_on is not None and self.expires_on <= self.signed_on:
            raise ValueError("a release expires after the day it was signed")
        return self

    @field_serializer("uses")
    def _uses_in_order(self, uses: frozenset[ConsentUse]) -> list[ConsentUse]:
        # A set has no order; dump it in the literal's order so a record's JSON is stable.
        return [u for u in CONSENT_USES if u in uses]


class ConsentVerdict(BaseModel):
    """What a service returns about a record: the verdict and its reason, never the record itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reason: ConsentRefusal | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> ConsentVerdict:
        if self.ok != (self.reason is None):
            raise ValueError("a verdict is ok with no reason, or refused with one")
        return self


def _refuse(reason: ConsentRefusal, detail: str) -> ConsentVerdict:
    return ConsentVerdict(ok=False, reason=reason, detail=detail)


def consent_valid(
    record: ConsentRecord,
    *,
    use: str,
    likeness: Likeness,
    at: datetime,
    delivery: str | None = None,
) -> ConsentVerdict:
    """Whether *record* covers *use* of the subject's *likeness* at *at*.

    *at* is a timezone-aware datetime the caller passes in, so the check never reads the clock. The
    record's dates are calendar days, compared with *at*'s day in UTC, so one instant gets one verdict
    wherever the caller runs. For a voice, *delivery* (``neutral``, ``warm``…) is required.

    The first failure wins, in this order: ``revoked``, ``not-yet-signed``, ``expired``,
    ``likeness-not-covered``, ``use-not-covered``, ``delivery-not-covered``. A use this version does
    not know is refused as not covered rather than raised, so a newer caller fails closed.

    Raises ``ValueError`` for a naive *at*, an unknown *likeness*, or a voice check with no *delivery*:
    those are caller bugs, not answers.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("consent_valid needs a timezone-aware `at`")
    if likeness not in LIKENESSES:
        raise ValueError(f"unknown likeness {likeness!r}; known: {', '.join(LIKENESSES)}")
    if likeness == "voice" and not delivery:
        raise ValueError("a voice check names the delivery")
    day = at.astimezone(UTC).date()

    if record.revocation is not None and day >= record.revocation.effective_on:
        return _refuse("revoked", f"the release was revoked, effective {record.revocation.effective_on.isoformat()}")
    if day < record.signed_on:
        return _refuse("not-yet-signed", f"the release is signed on {record.signed_on.isoformat()}")
    if record.expires_on is not None and day >= record.expires_on:
        return _refuse("expired", f"the release expired on {record.expires_on.isoformat()}")
    if not (record.face if likeness == "face" else record.voice):
        return _refuse("likeness-not-covered", f"the release does not cover the subject's {likeness}")
    if use not in record.uses:
        return _refuse("use-not-covered", f"the release does not cover the use {use!r}")
    if likeness == "voice" and delivery not in record.deliveries:
        return _refuse("delivery-not-covered", f"the release does not cover the voice delivery {delivery!r}")
    return ConsentVerdict(ok=True, detail=f"the release covers {use} of the subject's {likeness}")


def consent_matches(record: ConsentRecord, provenance: Provenance) -> bool:
    """Whether *record* is the release *provenance* names: a ``consented`` source, same release, same subject."""
    return (
        provenance.kind == "consented"
        and provenance.release_sha256 == record.release_sha256
        and provenance.subject.strip() == record.subject.strip()
    )


CONSENT_SCHEMA_TITLE = "argus-cortex consent"


def consent_schema() -> str:
    """The canonical JSON Schema for ``ConsentRecord`` and ``ConsentVerdict``, as committed in ``consent-schema.json``.

    Non-Python clients generate their types from that file. ``tests/test_consent.py`` fails if the
    committed file and this module drift; regenerate it with ``make schema``.
    """
    return render_schema([ConsentRecord, ConsentVerdict], title=CONSENT_SCHEMA_TITLE)
