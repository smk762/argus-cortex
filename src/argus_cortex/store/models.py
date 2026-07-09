"""Lineage entities — the ingestion API of the persistence layer.

These are **cortex-owned** so the dependency direction stays ``cortex → lens``,
never the reverse: cortex does not import argus-lens. A producer (lens emitting a
``CaptionResult``) maps its output onto :class:`Caption` at the integration
boundary — :meth:`Caption.from_caption_result` is the one-liner that does it by
duck-typing, so lens never has to know cortex exists.

The entities mirror the lineage DAG persisted by
:class:`~argus_cortex.store.lineage.PostgresLineageStore`::

    source_asset ─▶ caption(version, backend, profile, params)
                        │
                        ▼
                    human_edit ─▶ dataset_membership ─▶ training_run
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Mapping


class SourceAsset(BaseModel):
    """An input image/asset a caption is derived from.

    ``sha256`` is the content-identity key: assets are de-duplicated on it, so
    re-ingesting the same bytes reuses the existing row rather than forking the
    lineage. ``immich_id`` / ``uri`` are pointers to where the bytes live (we
    don't own them until the MinIO phase).
    """

    uri: str
    sha256: str | None = None
    immich_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Caption(BaseModel):
    """One captioning of an asset — a versioned, reproducible node in the DAG.

    ``backend`` / ``profile`` / ``params`` together capture *how* the caption was
    produced, which is what makes a re-caption diffable and a training set
    reproducible. ``final_caption`` plus a later :class:`HumanEdit` form the
    ``(model_caption, edited_caption)`` pair the summariser feedback loop trains on.
    """

    final_caption: str
    version: int = 1
    backend: str | None = None
    profile: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    variants: dict[str, str] = Field(default_factory=dict)
    raw_tags: str | None = None
    raw_prose: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_caption_result(
        cls,
        result: Any,
        *,
        version: int = 1,
        backend: str | None = None,
        profile: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Caption:
        """Map a lens-style ``CaptionResult`` onto a :class:`Caption` by duck-typing.

        Reads ``final_caption`` / ``caption_variants`` / ``raw_tags`` /
        ``raw_prose`` / ``backend_name`` / ``metadata`` via ``getattr`` so cortex
        takes **no** dependency on argus-lens: any object exposing those
        attributes works. Pass ``profile`` / ``params`` (the resolved target
        profile and backend params) explicitly — they live outside the result
        object but are essential for reproducibility.
        """
        return cls(
            final_caption=getattr(result, "final_caption", "") or "",
            version=version,
            backend=backend if backend is not None else (getattr(result, "backend_name", None) or None),
            profile=dict(profile or {}),
            params=dict(params or {}),
            variants=dict(getattr(result, "caption_variants", None) or {}),
            raw_tags=getattr(result, "raw_tags", None) or None,
            raw_prose=getattr(result, "raw_prose", None) or None,
            metadata=dict(getattr(result, "metadata", None) or {}),
        )


class HumanEdit(BaseModel):
    """A human correction of a :class:`Caption` — the feedback-loop signal.

    Captured verbatim so the delta from ``final_caption`` to ``edited_caption``
    is recoverable; ``editor`` / ``note`` are optional provenance.
    """

    edited_caption: str
    editor: str | None = None
    note: str | None = None


class TrainingRun(BaseModel):
    """A LoRA training run — the sink of the lineage DAG.

    Linking runs to the exact captions/edits that fed them (via
    :class:`dataset membership <SourceAsset>`) answers "which images/captions
    produced LoRA X?" and makes a run reproducible.
    """

    dataset: str
    base_model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
