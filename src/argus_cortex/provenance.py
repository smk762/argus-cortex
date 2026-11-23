"""Where a face, voice or image came from, and whether it may be shown beyond its owner's own screens.

argus-mask defined this shape for face sources (argus-mask#19, landed in argus-mask#57). Curator,
darkroom and quarry write or check the same records, so the shape lives here and each package
imports it instead of keeping a copy that drifts.

Two tiers:

- ``personal``: the user's own use, anything they may lawfully use in private (a cartoon, a film
  character, a character LoRA). Needs only ``source``. Never ``cleared``.
- Cleared (``stock``, ``synthetic``, ``consented``): the only kinds that may go in a repo, a test, a
  demo, a published recording or anything commercial. Each states its licence. ``consented`` also
  names its subject and binds the signed release by its sha256, never the document itself.

A ``consented`` subject is either the name as the release gives it, or the opaque **subject ref**
``consent:<release_sha256>`` (argus-cortex#163). The ref names no one: it is the release's own key,
which the provenance carries anyway, and unlike a hash of a name it cannot be reversed by guessing
names. Use the ref for anything that leaves the record holder (manifests, exports, logs):
:func:`pseudonymise` turns a named provenance into one, :func:`argus_cortex.consent.subject_ref`
gives a record's, and ``consent_matches`` accepts either form.

``curated`` is reserved: argus-mask refuses it until it defines what a curated face must prove, so
it is in the literal but not cleared.

A generated source may name its ``generator`` (model, model sha256, seed, prompt), as
argus-mask#26 defined; it is optional, and only ``synthetic`` and ``personal`` sources carry one.
A ``consented`` source's ``release_sha256`` is the key to its ``ConsentRecord``
(:mod:`argus_cortex.consent`), which decides whether it may be used.

The meaning of each field is argus-mask's (#19, #26). Change this module only together with it:
every package that stores a ``Provenance`` reads these fields.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from argus_cortex.wire import render_schema

ProvenanceKind = Literal["personal", "stock", "synthetic", "consented", "curated"]

PROVENANCE_KINDS: tuple[ProvenanceKind, ...] = get_args(ProvenanceKind)
CLEARED_KINDS: frozenset[ProvenanceKind] = frozenset({"stock", "synthetic", "consented"})
SHA256_PATTERN = r"^[0-9a-f]{64}$"
GENERATOR_KINDS: frozenset[ProvenanceKind] = frozenset({"synthetic", "personal"})
# A subject written as ``SUBJECT_REF_PREFIX + release_sha256`` names the release, not the person.
SUBJECT_REF_PREFIX = "consent:"


def subject_ref_of(release_sha256: str) -> str:
    """The opaque subject ref for the release *release_sha256*: ``consent:<release_sha256>``."""
    return SUBJECT_REF_PREFIX + release_sha256


class Generator(BaseModel):
    """How a generated source was made (argus-mask#26): the model, its sha256, the seed and the prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(..., min_length=1)
    model_sha256: str | None = Field(None, pattern=SHA256_PATTERN)
    seed: int | None = None
    prompt: str = ""

    @model_validator(mode="after")
    def _named(self) -> Generator:
        if not self.model.strip():
            raise ValueError("a generator names its model")
        return self


class LookAlike(BaseModel):
    """A look-alike check for a face shown publicly (argus-mask#176): who looked, when, how, and what they found.

    Cleared faces may be served without one; a demo or a published recording needs one. Until this field
    existed the same facts lived in ``notes`` (splat-01 … splat-03).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    who: str = Field(..., min_length=1, max_length=200, description="who checked")
    at: date = Field(..., description="the day of the check, UTC calendar date")
    how: str = Field(..., min_length=1, max_length=500, description="how they checked (tool, what was searched)")
    found: str = Field(..., min_length=1, max_length=2000, description="what they found")


class Provenance(BaseModel):
    """Where a source came from. Required wherever the suite serves or trains on a likeness.

    - ``personal``: the user's own use; needs only ``source``; not cleared to show or share.
    - ``stock``: a published asset under its licence. ``synthetic``: generated, with no real person
      or owned character going in.
    - ``consented``: a real person's likeness (or an owned character) under a signed release, bound
      by the release's sha256 (never the document) and its subject: the name as the release gives
      it, or the opaque ref ``consent:<release_sha256>``, which names no one.
    - ``curated``: reserved; not cleared.

    Unknown fields are refused, so a misspelt field cannot be dropped in silence (argus-mask#64).
    ``licence`` is read as ``license``, and the derived ``cleared`` / ``demo_ready`` a dumped record
    carries are dropped and recomputed, never believed.
    """

    # Frozen: the completeness check runs only at construction, so an assignment such as
    # ``p.kind = "consented"`` would otherwise yield a cleared record that skipped it.
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProvenanceKind
    source: str = Field(..., min_length=1)
    license: str = ""
    notes: str = ""
    subject: str = ""
    release_sha256: str | None = Field(None, pattern=SHA256_PATTERN)
    generator: Generator | None = None
    lookalike: LookAlike | None = None

    @model_validator(mode="before")
    @classmethod
    def _read(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        data = {k: v for k, v in data.items() if k not in ("cleared", "demo_ready")}
        if "licence" in data:
            licence = data.pop("licence")
            if "license" in data and data["license"] != licence:
                raise ValueError("give the licence once: `license` and `licence` disagree")
            data["license"] = licence
        return data

    @model_validator(mode="after")
    def _complete(self) -> Provenance:
        if not self.source.strip():
            raise ValueError("a source states where it came from")
        if self.kind != "personal" and not self.license.strip():
            raise ValueError(f"a {self.kind} source states its licence")
        if self.kind == "consented" and not (self.release_sha256 and self.subject.strip()):
            raise ValueError("a consented source names its subject and the sha256 of the signed release")
        subject = self.subject.strip()
        if subject.startswith(SUBJECT_REF_PREFIX) and (
            self.release_sha256 is None or subject != subject_ref_of(self.release_sha256)
        ):
            raise ValueError("a subject ref is consent:<release_sha256> of the source's own release")
        if self.generator is not None and self.kind not in GENERATOR_KINDS:
            raise ValueError(f"a {self.kind} source has no generator; only synthetic and personal sources do")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cleared(self) -> bool:
        """Whether the source may be shown beyond the user's own screens: a demo, a release, anything public."""
        return self.kind in CLEARED_KINDS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def demo_ready(self) -> bool:
        """Whether a cleared face may go in the hosted demo: needs a recorded look-alike check (argus-mask#176)."""
        return self.cleared and self.lookalike is not None


def pseudonymise(provenance: Provenance) -> Provenance:
    """A copy of the ``consented`` *provenance* whose ``subject`` is its release's subject ref, not a name.

    Only ``subject`` changes, and ``consent_matches`` still matches the copy to its record. ``source``,
    ``license`` and ``notes`` are free text and are kept as written, so a writer that puts a name there
    must leave it out itself. Raises ``ValueError`` for any other kind: only a ``consented`` source is
    bound to a person.
    """
    if provenance.kind != "consented" or provenance.release_sha256 is None:
        raise ValueError(f"only a consented source has a subject to pseudonymise, not a {provenance.kind} one")
    data = provenance.model_dump(exclude={"cleared", "demo_ready"})
    data["subject"] = subject_ref_of(provenance.release_sha256)
    return Provenance.model_validate(data)


PROVENANCE_SCHEMA_TITLE = "argus-cortex provenance"


def provenance_schema() -> str:
    """The canonical JSON Schema for ``Provenance``, as committed in ``provenance-schema.json``.

    Non-Python clients generate their types from that file. ``tests/test_provenance.py`` fails if
    the committed file and this module drift; regenerate it with ``make schema``.
    """
    return render_schema([Provenance], title=PROVENANCE_SCHEMA_TITLE)
