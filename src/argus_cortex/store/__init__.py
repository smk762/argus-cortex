"""Optional stateful services layer — the suite's persistent "memory".

External and opt-in: each store is addressed by a ``CORTEX_*`` URL in the
environment and no-ops when unset (see :class:`~argus_cortex.store.config.StoreConfig`).
Phase 1 ships the Postgres lineage store, phase 2 the Qdrant vector store; MinIO
blobs slot in later without reshaping this package.

The dependency direction is ``cortex → lens``: cortex defines the lineage
entities in :mod:`~argus_cortex.store.models` and the embedding model is the
caller's concern, so lens never imports cortex and stays DB-free.
"""

from __future__ import annotations

from argus_cortex.store.config import StoreConfig
from argus_cortex.store.errors import StoreError
from argus_cortex.store.lineage import (
    LineageStore,
    NullLineageStore,
    PostgresLineageStore,
    open_lineage_store,
)
from argus_cortex.store.models import (
    Caption,
    HumanEdit,
    SourceAsset,
    TrainingRun,
)
from argus_cortex.store.vector import (
    IMAGE_COLLECTION,
    TAGSET_COLLECTION,
    NullVectorStore,
    QdrantVectorStore,
    VectorHit,
    VectorStore,
    open_vector_store,
)

__all__ = [
    "StoreConfig",
    "StoreError",
    # lineage (phase 1: Postgres)
    "LineageStore",
    "NullLineageStore",
    "PostgresLineageStore",
    "open_lineage_store",
    "Caption",
    "HumanEdit",
    "SourceAsset",
    "TrainingRun",
    # vector (phase 2: Qdrant)
    "VectorStore",
    "NullVectorStore",
    "QdrantVectorStore",
    "VectorHit",
    "open_vector_store",
    "IMAGE_COLLECTION",
    "TAGSET_COLLECTION",
]
