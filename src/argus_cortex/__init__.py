"""argus-cortex — Shared foundation for the Argus suite: taxonomy, versioned wire-schema tooling, and the local/remote AI backend contract"""

from __future__ import annotations

try:
    # Written by hatch-vcs at build time (see pyproject [tool.hatch.build.hooks.vcs]).
    from argus_cortex._version import __version__
except ImportError:  # running from a source checkout that hasn't been built
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("argus-cortex")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"

from argus_cortex.backends import (
    Backend,
    BackendError,
    BackendKind,
    LocalBackend,
    RemoteBackend,
    RemoteProvider,
    resolve_device,
    with_retries,
)
from argus_cortex.store import (
    IMAGE_COLLECTION,
    TAGSET_COLLECTION,
    Caption,
    HumanEdit,
    LineageStore,
    NullLineageStore,
    NullVectorStore,
    PostgresLineageStore,
    QdrantVectorStore,
    SourceAsset,
    StoreConfig,
    StoreError,
    TrainingRun,
    VectorHit,
    VectorStore,
    open_lineage_store,
    open_vector_store,
)
from argus_cortex.taxonomy import TargetCategory, TargetProfile, TargetStyle
from argus_cortex.wire import (
    VersionError,
    check_version,
    make_versioned_base,
    render_schema,
    schema_major,
    wire_schema,
)

__all__ = [
    "__version__",
    # taxonomy
    "TargetProfile",
    "TargetCategory",
    "TargetStyle",
    # wire
    "VersionError",
    "check_version",
    "make_versioned_base",
    "render_schema",
    "schema_major",
    "wire_schema",
    # backends
    "Backend",
    "BackendError",
    "BackendKind",
    "LocalBackend",
    "RemoteBackend",
    "RemoteProvider",
    "resolve_device",
    "with_retries",
    # store (optional stateful services layer)
    "StoreConfig",
    "StoreError",
    "LineageStore",
    "NullLineageStore",
    "PostgresLineageStore",
    "open_lineage_store",
    "Caption",
    "HumanEdit",
    "SourceAsset",
    "TrainingRun",
    "VectorStore",
    "NullVectorStore",
    "QdrantVectorStore",
    "VectorHit",
    "open_vector_store",
    "IMAGE_COLLECTION",
    "TAGSET_COLLECTION",
]
