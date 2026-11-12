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

``curated`` is reserved: argus-mask refuses it until it defines what a curated face must prove, so
it is in the literal but not cleared.

The meaning of each field is argus-mask's (#19). Change this module only together with it: every
package that stores a ``Provenance`` reads these fields. Fields still open in argus-cortex#31 (a full
consent record with scope, expiry and revocation; the model, seed and prompt of a generated face)
are added here, additively, once argus-mask#19 defines them.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from argus_cortex.wire import render_schema

ProvenanceKind = Literal["personal", "stock", "synthetic", "consented", "curated"]

PROVENANCE_KINDS: tuple[ProvenanceKind, ...] = get_args(ProvenanceKind)
CLEARED_KINDS: frozenset[ProvenanceKind] = frozenset({"stock", "synthetic", "consented"})
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class Provenance(BaseModel):
    """Where a source came from. Required wherever the suite serves or trains on a likeness.

    - ``personal``: the user's own use; needs only ``source``; not cleared to show or share.
    - ``stock``: a published asset under its licence. ``synthetic``: generated, with no real person
      or owned character going in.
    - ``consented``: a real person's likeness (or an owned character) under a signed release, bound
      by the release's sha256 (never the document) and the subject as the release names them.
    - ``curated``: reserved; not cleared.
    """

    # Frozen: the completeness check runs only at construction, so an assignment such as
    # ``p.kind = "consented"`` would otherwise yield a cleared record that skipped it.
    model_config = ConfigDict(frozen=True)

    kind: ProvenanceKind
    source: str = Field(..., min_length=1)
    license: str = ""
    notes: str = ""
    subject: str = ""
    release_sha256: str | None = Field(None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _complete(self) -> Provenance:
        if self.kind != "personal" and not self.license.strip():
            raise ValueError(f"a {self.kind} source states its licence")
        if self.kind == "consented" and not (self.release_sha256 and self.subject.strip()):
            raise ValueError("a consented source names its subject and the sha256 of the signed release")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cleared(self) -> bool:
        """Whether the source may be shown beyond the user's own screens: a demo, a release, anything public."""
        return self.kind in CLEARED_KINDS


PROVENANCE_SCHEMA_TITLE = "argus-cortex provenance"


def provenance_schema() -> str:
    """The canonical JSON Schema for ``Provenance``, as committed in ``provenance-schema.json``.

    Non-Python clients generate their types from that file. ``tests/test_provenance.py`` fails if
    the committed file and this module drift; regenerate it with ``make schema``.
    """
    return render_schema([Provenance], title=PROVENANCE_SCHEMA_TITLE)
