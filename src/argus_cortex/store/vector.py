"""The vector store — image + tag-set embeddings in Qdrant (phase 2).

Phase 2 of the stateful services layer (see the tracking issue). Like the phase-1
lineage store it is **optional and degrades to a no-op**: :func:`open_vector_store`
returns a :class:`NullVectorStore` when no ``CORTEX_QDRANT_URL`` is configured, so
a caller writes the same ``store.upsert(...)`` / ``store.search(...)`` code whether
or not retrieval is switched on. When a URL *is* set, :class:`QdrantVectorStore`
talks to Qdrant via ``qdrant-client``, which lives behind the ``argus-cortex[qdrant]``
extra and is imported lazily (same discipline as ``[postgres]`` / ``[remote]``).

cortex stores and queries vectors it is **handed** — computing embeddings (the
model choice) is the caller's job, keeping the dependency direction ``cortex →
lens`` intact. Link a vector back to the lineage DAG by putting the phase-1
``caption_id`` / ``asset_id`` in its ``payload``; a search hit then joins straight
back to Postgres.

Everything is synchronous, matching the rest of cortex; from async code wrap a
call in ``asyncio.to_thread(...)``.
"""

from __future__ import annotations

import functools
import uuid
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from argus_cortex.store.config import StoreConfig
from argus_cortex.store.errors import require_extra, resolve_error_types, wrap_errors

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Canonical collection names for the two embedding kinds (callers may use others).
IMAGE_COLLECTION = "image_embeddings"
TAGSET_COLLECTION = "tagset_embeddings"

# Qdrant distance metrics, by their Qdrant enum value.
VectorDistance = Literal["Cosine", "Euclid", "Dot", "Manhattan"]

# Fixed namespace for deriving a Qdrant-valid UUID from an arbitrary string key.
_POINT_ID_NAMESPACE = uuid.UUID("a5c0ffee-0000-4000-8000-000000000001")


def normalise_point_id(point_id: str | int) -> str | int:
    """Return a point id Qdrant accepts (an unsigned int or a UUID string).

    Qdrant only allows an integer or a UUID as a point id, so an arbitrary string
    key (e.g. a lineage ``caption_id`` that happens not to be a UUID) is mapped
    **deterministically** to a UUID5 in :data:`_POINT_ID_NAMESPACE`. The mapping is
    stable, so re-upserting the same key overwrites the same point. Carry the
    original key in the payload if you need to read it back — the returned/searched
    ``VectorHit.id`` is this normalised id, not the original string.
    """
    if isinstance(point_id, int):
        return point_id
    try:
        return str(uuid.UUID(point_id))  # already a UUID -> pass through, normalised
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, point_id))


@functools.lru_cache(maxsize=1)
def _qdrant_error_types() -> tuple[type[BaseException], ...]:
    """qdrant-client's operational exceptions, so failures fold into StoreError.

    Cached once (the classes are process-global). qdrant-client's exception names
    have shifted across versions and the transport (REST/httpx vs gRPC) varies, so
    any name that can't be resolved is skipped. Empty only when neither
    qdrant-client nor httpx is importable, in which case the missing-driver path
    has already raised StoreError before any operation runs.
    """
    return resolve_error_types(
        (
            ("qdrant_client.http.exceptions", ("UnexpectedResponse", "ResponseHandlingException", "ApiException")),
            ("httpx", "HTTPError"),
        )
    )


class VectorHit(BaseModel):
    """One search result: the point id, its similarity score, and its payload.

    ``payload`` is where the lineage ids live, so a hit joins back to the phase-1
    Postgres rows (``caption_id`` → caption, ``asset_id`` → source_asset). Prefer
    joining on a payload id over ``id``: ``id`` is the string form of the Qdrant
    point id (an int comes back stringified, and a non-UUID key was normalised to
    a UUID5 at upsert), so it may not equal the key you passed.
    """

    id: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """The retrieval contract; both the Qdrant and no-op backends satisfy it."""

    @property
    def is_enabled(self) -> bool:
        """Whether vectors are actually persisted (``False`` for the null store)."""
        ...

    def ensure_collection(self, name: str, *, dim: int, distance: VectorDistance = "Cosine") -> None:
        """Create collection *name* with vector size *dim* if it doesn't exist (idempotent)."""
        ...

    def upsert(
        self,
        collection: str,
        *,
        point_id: str | int,
        vector: Sequence[float],
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Insert/overwrite one point (id + vector + payload) in *collection*."""
        ...

    def search(
        self,
        collection: str,
        *,
        vector: Sequence[float],
        top_k: int = 10,
        where: Mapping[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Return the *top_k* nearest points to *vector*, optionally payload-filtered."""
        ...

    def close(self) -> None:
        """Release the client."""
        ...


class NullVectorStore:
    """No-op store used when no Qdrant URL is configured — the graceful degrade.

    Writes do nothing and :meth:`search` returns empty, so retrieval simply
    no-ops when ``CORTEX_QDRANT_URL`` is unset instead of forcing callers to guard.
    """

    is_enabled = False

    def ensure_collection(self, name: str, *, dim: int, distance: VectorDistance = "Cosine") -> None:
        return None

    def upsert(
        self,
        collection: str,
        *,
        point_id: str | int,
        vector: Sequence[float],
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    def search(
        self,
        collection: str,
        *,
        vector: Sequence[float],
        top_k: int = 10,
        where: Mapping[str, Any] | None = None,
    ) -> list[VectorHit]:
        return []

    def close(self) -> None:
        return None


class QdrantVectorStore:
    """Persist + query embeddings in Qdrant via ``qdrant-client``.

    Point it at a Qdrant instance by URL (``http://host:6333``). The client and
    the qdrant ``models`` module are imported lazily on first use
    (``argus-cortex[qdrant]``); injected ``client`` / ``models`` (tests) bypass the
    import and the network entirely.
    """

    is_enabled = True

    def __init__(
        self,
        url: str,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
        client: Any = None,
        models: Any = None,
    ) -> None:
        self.url = url.rstrip("/") if url else url
        self.api_key = api_key
        self.timeout = timeout
        self._client_obj = client  # injectable qdrant client (tests)
        self._models_obj = models  # injectable qdrant.models namespace (tests)

    # -- client / models -------------------------------------------------------

    def _client(self) -> Any:
        if self._client_obj is None:
            qc = require_extra("qdrant_client", "qdrant", feature="qdrant vector store")
            kwargs: dict[str, Any] = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.timeout is not None:
                kwargs["timeout"] = self.timeout
            self._client_obj = qc.QdrantClient(url=self.url, **kwargs)
        return self._client_obj

    def _models(self) -> Any:
        if self._models_obj is None:
            self._models_obj = require_extra("qdrant_client.models", "qdrant", feature="qdrant vector store")
        return self._models_obj

    def _to_filter(self, where: Mapping[str, Any] | None) -> Any:
        """Turn a ``{field: value}`` exact-match mapping into a Qdrant Filter (or None)."""
        if not where:
            return None
        m = self._models()
        return m.Filter(must=[m.FieldCondition(key=k, match=m.MatchValue(value=v)) for k, v in where.items()])

    # -- operations ------------------------------------------------------------

    def ensure_collection(self, name: str, *, dim: int, distance: VectorDistance = "Cosine") -> None:
        def op() -> None:
            client = self._client()
            if client.collection_exists(name):
                return
            m = self._models()
            try:
                client.create_collection(
                    collection_name=name,
                    vectors_config=m.VectorParams(size=dim, distance=m.Distance(distance)),
                )
            except _qdrant_error_types() as exc:
                # Qdrant's create_collection isn't idempotent: lose a concurrent
                # create race and it errors. If the collection exists now, that's
                # the outcome we wanted; otherwise the failure is real — re-raise
                # (wrap_errors turns it into StoreError).
                if not client.collection_exists(name):
                    raise exc

        wrap_errors(op, errors=_qdrant_error_types(), label="ensure_collection")

    def upsert(
        self,
        collection: str,
        *,
        point_id: str | int,
        vector: Sequence[float],
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        # Qdrant only accepts an int or UUID id; map any other string key to a
        # stable UUID5 so a lineage caption_id/asset_id "just works" (see
        # normalise_point_id). Carry the original key in payload to read it back.
        def op() -> None:
            client = self._client()
            m = self._models()
            point = m.PointStruct(id=normalise_point_id(point_id), vector=list(vector), payload=dict(payload or {}))
            client.upsert(collection_name=collection, points=[point])

        wrap_errors(op, errors=_qdrant_error_types(), label="upsert")

    def search(
        self,
        collection: str,
        *,
        vector: Sequence[float],
        top_k: int = 10,
        where: Mapping[str, Any] | None = None,
    ) -> list[VectorHit]:
        def op() -> list[VectorHit]:
            client = self._client()
            resp = client.query_points(
                collection_name=collection,
                query=list(vector),
                limit=top_k,
                query_filter=self._to_filter(where),
            )
            # query_points returns a QueryResponse(.points); tolerate a bare list,
            # and a None .points (empty/error variant) collapses to no hits.
            points = getattr(resp, "points", resp) or []
            return [
                VectorHit(
                    id=str(p.id),
                    score=float(p.score) if p.score is not None else 0.0,
                    payload=dict(p.payload or {}),
                )
                for p in points
            ]

        return wrap_errors(op, errors=_qdrant_error_types(), label="search")

    def close(self) -> None:
        if self._client_obj is not None:
            close = getattr(self._client_obj, "close", None)
            if callable(close):
                close()
            self._client_obj = None


def open_vector_store(config: StoreConfig | None = None) -> VectorStore:
    """Return a live :class:`QdrantVectorStore`, or a :class:`NullVectorStore`.

    The single entry point callers use. With no *config*, reads
    :meth:`StoreConfig.from_env <argus_cortex.store.config.StoreConfig.from_env>`;
    when ``qdrant_url`` is unset the returned store no-ops, so retrieval is opt-in
    by simply setting ``CORTEX_QDRANT_URL``.
    """
    if config is None:
        config = StoreConfig.from_env()
    if config.qdrant_url:
        return QdrantVectorStore(config.qdrant_url)
    return NullVectorStore()
