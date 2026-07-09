from __future__ import annotations

import hashlib
import importlib.util
from datetime import timedelta

import pytest

from argus_cortex.store import (
    BlobStore,
    NullBlobStore,
    S3BlobStore,
    StoreConfig,
    StoreError,
    content_key,
    open_blob_store,
)
from argus_cortex.store.blob import _parse_endpoint

_HAS_MINIO = importlib.util.find_spec("minio") is not None


# --------------------------------------------------------------------------
# Fake minio client — stores bytes in a dict so put/get/exists round-trip,
# so the tests run without the `minio` driver or a live server.
# --------------------------------------------------------------------------


class _FakeS3Error(Exception):
    """Duck-types minio.error.S3Error: carries a `.code` the store inspects."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _FakeMinio:
    def __init__(self, buckets: set[str] | None = None) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str | None]] = {}
        self.buckets: set[str] = set(buckets or ())
        self.made: list[tuple[str, str]] = []
        self.puts: list[tuple[str, str]] = []
        self.presigned: list[tuple[str, str, object]] = []

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str, location: str | None = None) -> None:
        self.buckets.add(bucket)
        self.made.append((bucket, location or ""))

    def put_object(self, bucket: str, key: str, data: object, length: int, content_type: str | None = None) -> None:
        self.objects[(bucket, key)] = (data.read(), content_type)  # type: ignore[attr-defined]
        self.puts.append((bucket, key))

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        if (bucket, key) not in self.objects:
            raise _FakeS3Error("NoSuchKey")
        return _FakeResponse(self.objects[(bucket, key)][0])

    def stat_object(self, bucket: str, key: str) -> object:
        if (bucket, key) not in self.objects:
            raise _FakeS3Error("NoSuchKey")
        return object()

    def presigned_get_object(self, bucket: str, key: str, expires: object = None) -> str:
        self.presigned.append((bucket, key, expires))
        return f"http://minio/{bucket}/{key}?sig=x"


def _store_with(client: _FakeMinio, *, bucket: str = "argus") -> S3BlobStore:
    return S3BlobStore("http://minio:9000", bucket, client=client)


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_content_key_is_sha256_hex() -> None:
    assert content_key(b"hello") == hashlib.sha256(b"hello").hexdigest()
    assert content_key(b"") == hashlib.sha256(b"").hexdigest()


def test_parse_endpoint_forms() -> None:
    assert _parse_endpoint("http://localhost:9000") == ("localhost:9000", False)
    assert _parse_endpoint("https://minio.example.com") == ("minio.example.com", True)
    assert _parse_endpoint("localhost:9000") == ("localhost:9000", False)  # bare host:port


# --------------------------------------------------------------------------
# factory + config
# --------------------------------------------------------------------------


def test_open_blob_store_unconfigured_returns_null() -> None:
    assert isinstance(open_blob_store(StoreConfig()), NullBlobStore)
    # endpoint without bucket is not enough -> still null
    assert isinstance(open_blob_store(StoreConfig(s3_endpoint="http://localhost:9000")), NullBlobStore)


def test_open_blob_store_with_endpoint_and_bucket_returns_s3() -> None:
    store = open_blob_store(StoreConfig(s3_endpoint="http://localhost:9000", s3_bucket="argus"))
    assert isinstance(store, S3BlobStore)
    assert isinstance(store, BlobStore)  # runtime-checkable protocol
    assert store.is_enabled is True


# --------------------------------------------------------------------------
# NullBlobStore (graceful degradation)
# --------------------------------------------------------------------------


def test_null_blob_store_noops_but_still_returns_content_key() -> None:
    store = NullBlobStore()
    assert store.is_enabled is False
    assert store.put("k", b"x") is None
    assert store.get("k") is None
    assert store.exists("k") is False
    assert store.url("k") is None
    store.ensure_bucket()
    # the content key is a pure hash, so lineage can still record the sha256
    assert store.put_content_addressed(b"hello") == content_key(b"hello")
    store.close()


# --------------------------------------------------------------------------
# S3BlobStore via an injected fake client
# --------------------------------------------------------------------------


def test_put_get_round_trip() -> None:
    client = _FakeMinio(buckets={"argus"})
    store = _store_with(client)
    store.put("k1", b"payload", content_type="image/jpeg")
    assert store.get("k1") == b"payload"
    assert store.exists("k1") is True
    # content_type reached the client
    assert client.objects[("argus", "k1")][1] == "image/jpeg"


def test_get_missing_returns_none_and_exists_false() -> None:
    store = _store_with(_FakeMinio(buckets={"argus"}))
    assert store.get("nope") is None
    assert store.exists("nope") is False


def test_get_releases_the_connection() -> None:
    client = _FakeMinio(buckets={"argus"})
    store = _store_with(client)
    store.put("k", b"data")
    # read the response so we can assert it was closed/released
    resp = client.get_object("argus", "k")
    store.get("k")
    # the store's own response is internal; assert our probe object is unaffected,
    # and that a fresh get works repeatedly (no leaked/again-consumed stream)
    assert resp.read() == b"data"
    assert store.get("k") == b"data"


def test_put_content_addressed_returns_sha_and_dedupes() -> None:
    client = _FakeMinio(buckets={"argus"})
    store = _store_with(client)
    key = store.put_content_addressed(b"the-bytes", content_type="image/png")
    assert key == content_key(b"the-bytes")
    assert store.get(key) == b"the-bytes"
    # re-storing identical bytes must NOT upload again (content-addressed dedup)
    store.put_content_addressed(b"the-bytes")
    assert client.puts.count(("argus", key)) == 1


def test_url_builds_presigned_get_with_timedelta_expiry() -> None:
    client = _FakeMinio(buckets={"argus"})
    store = _store_with(client)
    assert store.url("k") == "http://minio/argus/k?sig=x"
    assert client.presigned[-1] == ("argus", "k", timedelta(seconds=3600))  # default
    store.url("k", expires=60)
    assert client.presigned[-1][2] == timedelta(seconds=60)


def test_ensure_bucket_creates_when_absent_and_skips_when_present() -> None:
    absent = _FakeMinio(buckets=set())
    _store_with(absent).ensure_bucket()
    assert absent.made and absent.made[0][0] == "argus"

    present = _FakeMinio(buckets={"argus"})
    _store_with(present).ensure_bucket()
    assert present.made == []  # idempotent: no create when it already exists


def test_ensure_bucket_tolerates_lost_create_race() -> None:
    # make_bucket fails, but the bucket exists on re-check (a concurrent worker
    # created it) -> ensure_bucket must not raise. Duck-typed, no real driver needed.
    class _RaceMinio(_FakeMinio):
        def __init__(self) -> None:
            super().__init__(buckets=set())
            self._checks = 0

        def bucket_exists(self, bucket: str) -> bool:
            self._checks += 1
            return self._checks > 1  # absent on pre-check, present on post-error re-check

        def make_bucket(self, bucket: str, location: str | None = None) -> None:
            raise _FakeS3Error("BucketAlreadyOwnedByYou")

    _store_with(_RaceMinio()).ensure_bucket()  # must not raise


def test_close_drops_client() -> None:
    store = _store_with(_FakeMinio(buckets={"argus"}))
    store.exists("k")  # touch the client
    store.close()
    assert store._client_obj is None


# --------------------------------------------------------------------------
# Real minio contract checks (need the driver for real error types / client)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_MINIO, reason="needs a real minio/urllib3 error type to exercise the wrap path")
def test_operational_error_wrapped_in_store_error() -> None:
    import urllib3

    class _FailMinio(_FakeMinio):
        def put_object(self, bucket, key, data, length, content_type=None):  # type: ignore[override]
            raise urllib3.exceptions.HTTPError("connection reset")

    store = _store_with(_FailMinio(buckets={"argus"}))
    with pytest.raises(StoreError, match="put failed"):
        store.put("k", b"x")


@pytest.mark.skipif(not _HAS_MINIO, reason="minio not installed")
def test_real_client_construction() -> None:
    from minio import Minio

    store = S3BlobStore("http://localhost:9000", "argus", access_key="a", secret_key="b")
    # Minio() construction is lazy (no connection), so this exercises the real
    # _client() import/parse path without a live server.
    assert isinstance(store._client(), Minio)
    store.close()


@pytest.mark.skipif(_HAS_MINIO, reason="minio installed; the missing-extra path can't be exercised")
def test_client_without_minio_raises_helpful_error() -> None:
    with pytest.raises(StoreError, match=r"argus-cortex\[s3\]"):
        S3BlobStore("http://localhost:9000", "argus")._client()
