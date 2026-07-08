"""Optional stateful services layer — the suite's persistent "memory".

External and opt-in: each store is addressed by a ``CORTEX_*`` URL in the
environment and no-ops when unset (see :class:`~argus_cortex.store.config.StoreConfig`).
Phase 1 ships the Postgres lineage store; Qdrant embeddings and MinIO blobs slot
in later without reshaping this package.

The dependency direction is ``cortex → lens``: cortex defines the lineage
entities in :mod:`~argus_cortex.store.models` and a producer maps its output onto
them, so lens never imports cortex and stays DB-free.
"""

from __future__ import annotations

from argus_cortex.store.config import StoreConfig
from argus_cortex.store.lineage import (
    LineageStore,
    NullLineageStore,
    PostgresLineageStore,
    StoreError,
    open_lineage_store,
)
from argus_cortex.store.models import (
    Caption,
    HumanEdit,
    SourceAsset,
    TrainingRun,
)

__all__ = [
    "StoreConfig",
    "LineageStore",
    "NullLineageStore",
    "PostgresLineageStore",
    "StoreError",
    "open_lineage_store",
    "Caption",
    "HumanEdit",
    "SourceAsset",
    "TrainingRun",
]
