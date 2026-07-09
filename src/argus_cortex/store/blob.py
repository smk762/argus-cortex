"""The blob store — own image bytes in MinIO / S3 (phase 3).

Phase 3 of the stateful services layer (see the tracking issue). Like the earlier
stores it is **optional and degrades to a no-op**: :func:`open_blob_store` returns
a :class:`NullBlobStore` when no S3 endpoint/bucket is configured, so a caller
writes the same ``store.put(...)`` / ``store.get(...)`` code whether or not blob
storage is switched on. When configured, :class:`S3BlobStore` talks to an
S3-compatible service (MinIO, AWS S3, …) via ``minio``, which lives behind the
``argus-cortex[s3]`` extra and is imported lazily (same discipline as
``[postgres]`` / ``[qdrant]``).

Blobs are **content-addressed on sha256** (:func:`content_key`), the same identity
as ``source_asset.sha256`` in the phase-1 lineage — so owning the bytes, de-duping
them, and joining them back to a caption all key off one hash. This store is only
for the export/selected path where cortex must own bytes; ``SourceAsset`` keeps
pointing at the filesystem / Immich by default.

Everything is synchronous, matching the rest of cortex; from async code wrap a
call in ``asyncio.to_thread(...)``.
"""

from __future__ import annotations

import functools
import hashlib
import io
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from argus_cortex.store.config import StoreConfig
from argus_cortex.store.errors import require_extra, wrap_errors

# S3 error codes that mean "the object/bucket isn't there" — a normal absence
# (return False/None), not an operational failure to wrap in StoreError.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NoSuchObjectKey", "NoSuchBucket", "NoSuchUpload"})

_DEFAULT_CONTENT_TYPE = "application/octet-stream"
_DEFAULT_REGION = "us-east-1"


def content_key(data: bytes) -> str:
    """The content-addressed storage key for *data*: its sha256 hex digest.

    The same value phase 1 stores as ``source_asset.sha256``, so a blob, its
    lineage row, and its vector payload all share one identity.
    """
    return hashlib.sha256(data).hexdigest()


def _parse_endpoint(endpoint: str) -> tuple[str, bool]:
    """Split a ``CORTEX_S3_ENDPOINT`` into minio's ``(host:port, secure)`` form.

    ``http://host:9000`` → ``("host:9000", False)``; ``https://host`` →
    ``("host", True)``; a bare ``host:9000`` (no scheme) → ``("host:9000", False)``.
    """
    if "://" not in endpoint:
        return endpoint, False
    parsed = urlparse(endpoint)
    return parsed.netloc, parsed.scheme == "https"


def _is_not_found(exc: BaseException) -> bool:
    """Whether *exc* is an S3 "no such object/bucket" error (duck-typed on ``code``)."""
    return getattr(exc, "code", None) in _NOT_FOUND_CODES


@functools.lru_cache(maxsize=1)
def _s3_error_types() -> tuple[type[BaseException], ...]:
    """minio's operational exceptions, so failures fold into StoreError.

    Cached once (classes are process-global) and imported lazily. ``S3Error``
    (bad response) derives from ``MinioException``; ``urllib3.HTTPError`` covers
    transport failures. Empty only when neither is importable, in which case the
    missing-driver path has already raised StoreError before any operation runs.
    """
    errs: list[type[BaseException]] = []
    try:
        from minio.error import MinioException

        errs.append(MinioException)
    except ImportError:  # pragma: no cover - no driver means no operational errors to wrap
        pass
    try:
        import urllib3

        errs.append(urllib3.exceptions.HTTPError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(errs)


@runtime_checkable
class BlobStore(Protocol):
    """The blob contract; both the S3 and no-op backends satisfy it."""

    @property
    def is_enabled(self) -> bool:
        """Whether bytes are actually persisted (``False`` for the null store)."""
        ...

    def ensure_bucket(self) -> None:
        """Create the configured bucket if it doesn't exist (idempotent)."""
        ...

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        """Store *data* at *key* (overwriting)."""
        ...

    def get(self, key: str) -> bytes | None:
        """Return the bytes at *key*, or ``None`` if absent / not configured."""
        ...

    def exists(self, key: str) -> bool:
        """Whether *key* is present."""
        ...

    def url(self, key: str, *, expires: int = 3600) -> str | None:
        """A presigned GET URL for *key* (``expires`` seconds), or ``None`` if disabled."""
        ...

    def put_content_addressed(self, data: bytes, *, content_type: str | None = None) -> str:
        """Store *data* under its :func:`content_key` (skipping the write if present); return the key."""
        ...

    def close(self) -> None:
        """Release the client."""
        ...


class NullBlobStore:
    """No-op store used when no S3 endpoint/bucket is configured — the graceful degrade.

    Writes do nothing, reads return ``None``/``False``. :meth:`put_content_addressed`
    still returns the content key (a pure hash), so a caller can record the sha256
    in lineage even when blob storage is off.
    """

    is_enabled = False

    def ensure_bucket(self) -> None:
        return None

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        return None

    def get(self, key: str) -> bytes | None:
        return None

    def exists(self, key: str) -> bool:
        return False

    def url(self, key: str, *, expires: int = 3600) -> str | None:
        return None

    def put_content_addressed(self, data: bytes, *, content_type: str | None = None) -> str:
        return content_key(data)

    def close(self) -> None:
        return None


class S3BlobStore:
    """Own image bytes in an S3-compatible service (MinIO / AWS S3) via ``minio``.

    Construct from an endpoint URL + bucket (+ optional credentials). The client is
    imported/created lazily on first use (``argus-cortex[s3]``); an injected
    ``client`` (tests) bypasses the import and the network entirely.
    """

    is_enabled = True

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        *,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        client: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self._client_obj = client  # injectable minio client (tests)

    # -- client ----------------------------------------------------------------

    def _client(self) -> Any:
        if self._client_obj is None:
            minio = require_extra("minio", "s3", feature="s3 blob store")
            host, secure = _parse_endpoint(self.endpoint)
            self._client_obj = minio.Minio(
                host,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=secure,
                region=self.region,
            )
        return self._client_obj

    # -- operations ------------------------------------------------------------

    def ensure_bucket(self) -> None:
        def op() -> None:
            client = self._client()
            if client.bucket_exists(self.bucket):
                return
            try:
                client.make_bucket(self.bucket, location=self.region or _DEFAULT_REGION)
            except Exception:
                # make_bucket isn't idempotent; if a concurrent worker created the
                # bucket, that's the outcome we wanted, otherwise the error is real.
                if not client.bucket_exists(self.bucket):
                    raise

        wrap_errors(op, errors=_s3_error_types(), label="ensure_bucket")

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        def op() -> None:
            client = self._client()
            client.put_object(
                self.bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type or _DEFAULT_CONTENT_TYPE,
            )

        wrap_errors(op, errors=_s3_error_types(), label="put")

    def get(self, key: str) -> bytes | None:
        def op() -> bytes | None:
            client = self._client()
            try:
                resp = client.get_object(self.bucket, key)
            except Exception as exc:
                if _is_not_found(exc):  # a missing object is None, not an error
                    return None
                raise
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        return wrap_errors(op, errors=_s3_error_types(), label="get")

    def exists(self, key: str) -> bool:
        def op() -> bool:
            client = self._client()
            try:
                client.stat_object(self.bucket, key)
                return True
            except Exception as exc:
                if _is_not_found(exc):  # a missing object is False, not an error
                    return False
                raise

        return wrap_errors(op, errors=_s3_error_types(), label="exists")

    def url(self, key: str, *, expires: int = 3600) -> str | None:
        def op() -> str:
            client = self._client()
            return client.presigned_get_object(self.bucket, key, expires=timedelta(seconds=expires))

        return wrap_errors(op, errors=_s3_error_types(), label="url")

    def put_content_addressed(self, data: bytes, *, content_type: str | None = None) -> str:
        # De-dupe: identical bytes hash to the same key, so skip the upload if it's
        # already there. Returns the sha256 key (== source_asset.sha256).
        key = content_key(data)
        if not self.exists(key):
            self.put(key, data, content_type=content_type)
        return key

    def close(self) -> None:
        self._client_obj = None


def open_blob_store(config: StoreConfig | None = None) -> BlobStore:
    """Return a live :class:`S3BlobStore`, or a :class:`NullBlobStore`.

    The single entry point callers use. With no *config*, reads
    :meth:`StoreConfig.from_env <argus_cortex.store.config.StoreConfig.from_env>`;
    the store is enabled only when both ``s3_endpoint`` and ``s3_bucket`` are set,
    so blob storage is opt-in by configuring ``CORTEX_S3_ENDPOINT`` / ``CORTEX_S3_BUCKET``.
    """
    if config is None:
        config = StoreConfig.from_env()
    if config.s3_endpoint and config.s3_bucket:
        return S3BlobStore(
            config.s3_endpoint,
            config.s3_bucket,
            access_key=config.s3_access_key,
            secret_key=config.s3_secret_key,
        )
    return NullBlobStore()
