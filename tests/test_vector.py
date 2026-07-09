from __future__ import annotations

import importlib.util
import uuid

import pytest

from argus_cortex.store import (
    IMAGE_COLLECTION,
    NullVectorStore,
    QdrantVectorStore,
    StoreConfig,
    StoreError,
    VectorHit,
    VectorStore,
    open_vector_store,
)
from argus_cortex.store.vector import VectorDistance, normalise_point_id

_HAS_QDRANT = importlib.util.find_spec("qdrant_client") is not None


# --------------------------------------------------------------------------
# Fakes — a qdrant client + a qdrant.models namespace, so the tests exercise
# the store's logic without qdrant-client installed or a live Qdrant.
# --------------------------------------------------------------------------


class _FakePoint:
    def __init__(self, **kw: object) -> None:
        self.kw = kw


class _FakeFilter:
    def __init__(self, must: list) -> None:
        self.must = must


class _FakeFieldCondition:
    def __init__(self, key: str, match: object) -> None:
        self.key = key
        self.match = match


class _FakeMatch:
    def __init__(self, value: object) -> None:
        self.value = value


class _FakeVectorParams:
    def __init__(self, size: int, distance: object) -> None:
        self.size = size
        self.distance = distance


class _FakeModels:
    Distance = staticmethod(lambda d: d)  # Distance("Cosine") -> "Cosine"
    PointStruct = _FakePoint
    Filter = _FakeFilter
    FieldCondition = _FakeFieldCondition
    MatchValue = _FakeMatch
    VectorParams = _FakeVectorParams


class _FakeScored:
    def __init__(self, id: str, score: float, payload: dict) -> None:
        self.id = id
        self.score = score
        self.payload = payload


class _FakeQueryResponse:
    def __init__(self, points: list) -> None:
        self.points = points


class _FakeQdrant:
    def __init__(self, *, exists: bool = False, hits: list | None = None) -> None:
        self.exists = exists
        self.hits = hits or []
        self.created: list = []
        self.upserts: list = []
        self.searches: list = []
        self.closed = False

    def collection_exists(self, name: str) -> bool:
        return self.exists

    def create_collection(self, collection_name: str, vectors_config: object) -> None:
        self.created.append((collection_name, vectors_config))

    def upsert(self, collection_name: str, points: list) -> None:
        self.upserts.append((collection_name, points))

    def query_points(self, collection_name: str, query: list, limit: int, query_filter: object) -> _FakeQueryResponse:
        self.searches.append(
            {
                "collection": collection_name,
                "query": query,
                "limit": limit,
                "filter": query_filter,
            }
        )
        return _FakeQueryResponse(self.hits)

    def close(self) -> None:
        self.closed = True


def _store_with(client: _FakeQdrant) -> QdrantVectorStore:
    return QdrantVectorStore("http://qdrant:6333/", client=client, models=_FakeModels)


# --------------------------------------------------------------------------
# factory + config
# --------------------------------------------------------------------------


def test_open_vector_store_no_url_returns_null() -> None:
    store = open_vector_store(StoreConfig())
    assert isinstance(store, NullVectorStore)
    assert isinstance(store, VectorStore)  # satisfies the runtime-checkable protocol
    assert store.is_enabled is False


def test_open_vector_store_with_url_returns_qdrant() -> None:
    store = open_vector_store(StoreConfig(qdrant_url="http://localhost:6333"))
    assert isinstance(store, QdrantVectorStore)
    assert store.is_enabled is True
    # trailing slash normalised
    assert store.url == "http://localhost:6333"


# --------------------------------------------------------------------------
# NullVectorStore (graceful degradation)
# --------------------------------------------------------------------------


def test_null_vector_store_noops() -> None:
    store = NullVectorStore()
    assert store.ensure_collection("c", dim=8) is None
    assert store.upsert("c", point_id="p", vector=[0.1, 0.2]) is None
    assert store.search("c", vector=[0.1, 0.2]) == []
    store.close()


# --------------------------------------------------------------------------
# QdrantVectorStore via injected client + models
# --------------------------------------------------------------------------


def test_ensure_collection_creates_when_absent() -> None:
    client = _FakeQdrant(exists=False)
    store = _store_with(client)
    store.ensure_collection(IMAGE_COLLECTION, dim=512, distance="Cosine")
    assert len(client.created) == 1
    name, params = client.created[0]
    assert name == IMAGE_COLLECTION
    assert params.size == 512
    assert params.distance == "Cosine"


def test_ensure_collection_skips_when_present() -> None:
    client = _FakeQdrant(exists=True)
    store = _store_with(client)
    store.ensure_collection(IMAGE_COLLECTION, dim=512)
    assert client.created == []  # idempotent: no create when it already exists


def test_upsert_passes_uuid_point_id_through() -> None:
    client = _FakeQdrant()
    store = _store_with(client)
    uid = "eb49211d-ac1c-49e9-aae3-e40ecc0a00dd"  # a Postgres caption_id is a UUID
    store.upsert(IMAGE_COLLECTION, point_id=uid, vector=[0.1, 0.2, 0.3], payload={"caption_id": uid})
    coll, points = client.upserts[-1]
    assert coll == IMAGE_COLLECTION
    assert points[0].kw == {"id": uid, "vector": [0.1, 0.2, 0.3], "payload": {"caption_id": uid}}


def test_upsert_maps_non_uuid_key_to_stable_uuid5() -> None:
    # Qdrant rejects arbitrary string ids, so a non-UUID key is normalised to a
    # deterministic UUID5; the original key belongs in the payload for join-back.
    client = _FakeQdrant()
    store = _store_with(client)
    store.upsert(IMAGE_COLLECTION, point_id="cap-1", vector=[0.1], payload={"caption_id": "cap-1"})
    stored_id = client.upserts[-1][1][0].kw["id"]
    assert stored_id == normalise_point_id("cap-1")  # deterministic
    assert uuid.UUID(stored_id)  # a valid UUID Qdrant will accept
    assert stored_id != "cap-1"


def test_upsert_passes_int_point_id_through() -> None:
    client = _FakeQdrant()
    store = _store_with(client)
    store.upsert(IMAGE_COLLECTION, point_id=7, vector=[0.0])
    assert client.upserts[-1][1][0].kw["id"] == 7  # ints are valid Qdrant ids
    assert client.upserts[-1][1][0].kw["payload"] == {}


def test_normalise_point_id_forms() -> None:
    uid = "eb49211d-ac1c-49e9-aae3-e40ecc0a00dd"
    assert normalise_point_id(7) == 7
    assert normalise_point_id(uid) == uid  # already valid -> pass through
    assert normalise_point_id("cap-1") == normalise_point_id("cap-1")  # stable
    assert normalise_point_id("cap-1") != normalise_point_id("cap-2")


def test_search_returns_hits_and_passes_filter() -> None:
    client = _FakeQdrant(hits=[_FakeScored("cap-2", 0.91, {"caption_id": "cap-2"})])
    store = _store_with(client)
    hits = store.search(IMAGE_COLLECTION, vector=[0.1, 0.2], top_k=3, where={"target_style": "photo"})
    assert hits == [VectorHit(id="cap-2", score=0.91, payload={"caption_id": "cap-2"})]
    call = client.searches[-1]
    assert call["limit"] == 3
    # the where-mapping became an exact-match Filter
    flt = call["filter"]
    assert flt.must[0].key == "target_style"
    assert flt.must[0].match.value == "photo"


def test_search_without_where_sends_no_filter() -> None:
    client = _FakeQdrant(hits=[])
    store = _store_with(client)
    assert store.search(IMAGE_COLLECTION, vector=[0.1]) == []
    assert client.searches[-1]["filter"] is None


def test_search_tolerates_bare_list_response() -> None:
    # older qdrant-client returned a bare list from search(); getattr(resp,"points",resp) covers it
    class _ListClient(_FakeQdrant):
        def query_points(self, collection_name, query, limit, query_filter):  # type: ignore[override]
            return [_FakeScored("x", 0.5, {})]

    store = _store_with(_ListClient())
    hits = store.search(IMAGE_COLLECTION, vector=[0.1])
    assert hits == [VectorHit(id="x", score=0.5, payload={})]


def test_search_tolerates_none_points() -> None:
    # a QueryResponse whose .points is None (empty/error variant) -> no hits, no crash
    class _NoneClient(_FakeQdrant):
        def query_points(self, collection_name, query, limit, query_filter):  # type: ignore[override]
            return _FakeQueryResponse(None)  # type: ignore[arg-type]

    assert _store_with(_NoneClient()).search(IMAGE_COLLECTION, vector=[0.1]) == []


def test_search_coerces_non_str_id_null_payload_and_null_score() -> None:
    # int id -> str, None payload -> {}, and a None score -> 0.0 (must not raise)
    client = _FakeQdrant(hits=[_FakeScored(42, None, None)])  # type: ignore[arg-type]
    store = _store_with(client)
    hits = store.search(IMAGE_COLLECTION, vector=[0.1])
    assert hits == [VectorHit(id="42", score=0.0, payload={})]


def test_close_closes_client() -> None:
    client = _FakeQdrant()
    store = _store_with(client)
    store.ensure_collection(IMAGE_COLLECTION, dim=4)  # touch the client
    store.close()
    assert client.closed is True
    assert store._client_obj is None


@pytest.mark.skipif(_HAS_QDRANT, reason="qdrant-client installed; the missing-extra path can't be exercised")
def test_client_without_qdrant_raises_helpful_error() -> None:
    with pytest.raises(StoreError, match=r"argus-cortex\[qdrant\]"):
        QdrantVectorStore("http://qdrant:6333")._client()


# --------------------------------------------------------------------------
# ensure_collection race tolerance (needs a real driver-error type to catch)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_QDRANT, reason="needs a real qdrant/httpx error type to exercise the race path")
def test_ensure_collection_tolerates_lost_create_race() -> None:
    import httpx

    class _RaceClient(_FakeQdrant):
        def __init__(self) -> None:
            super().__init__(exists=False)
            self._checks = 0

        def collection_exists(self, name: str) -> bool:
            # absent on the pre-check, present on the post-error re-check (a
            # concurrent worker created it) -> ensure_collection must not raise.
            self._checks += 1
            return self._checks > 1

        def create_collection(self, collection_name: str, vectors_config: object) -> None:
            raise httpx.HTTPError("collection already exists")

    _store_with(_RaceClient()).ensure_collection(IMAGE_COLLECTION, dim=4)  # must not raise


@pytest.mark.skipif(not _HAS_QDRANT, reason="needs a real qdrant/httpx error type to exercise the raise path")
def test_ensure_collection_reraises_when_still_absent() -> None:
    import httpx

    class _FailClient(_FakeQdrant):
        def create_collection(self, collection_name: str, vectors_config: object) -> None:
            raise httpx.HTTPError("boom")

    # create fails and the collection is still absent -> surfaces as StoreError
    store = _store_with(_FailClient())  # collection_exists stays False
    with pytest.raises(StoreError, match="ensure_collection failed"):
        store.ensure_collection(IMAGE_COLLECTION, dim=4)


# --------------------------------------------------------------------------
# Real qdrant-client contract checks (catch enum/param drift; no live server)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_QDRANT, reason="qdrant-client not installed")
def test_vector_distance_literals_are_valid_qdrant_enum_members() -> None:
    from typing import get_args

    from qdrant_client import models

    for value in get_args(VectorDistance):
        assert models.Distance(value)  # construct-by-value must not raise


@pytest.mark.skipif(not _HAS_QDRANT, reason="qdrant-client not installed")
def test_real_client_construction_uses_models_and_client() -> None:
    from qdrant_client import QdrantClient

    store = QdrantVectorStore("http://localhost:6333")
    # QdrantClient construction is lazy (no connection), so this exercises the
    # real _client()/_models() import paths without needing a live server.
    assert isinstance(store._client(), QdrantClient)
    assert store._models() is not None
    store.close()
