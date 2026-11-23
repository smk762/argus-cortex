"""Configuration for the optional stateful services layer.

The stores are **external and optional**: each is addressed by a URL/credential
read from the environment (a ``.env``), and an unset value means "off". Nothing
here connects to anything — it just captures which services are configured so a
caller can decide whether a store is available or should degrade to a no-op.

Only Postgres (``pg_url``) has a store implementation today (see
:mod:`argus_cortex.store.lineage`); the Qdrant / S3 fields are declared so the
``.env`` contract is complete and the later phases (embeddings, blobs) slot in
without reshaping config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, SecretStr

from argus_cortex import settings

if TYPE_CHECKING:
    from collections.abc import Mapping


def secret_value(value: SecretStr | None) -> str | None:
    """The plain string inside a :class:`~pydantic.SecretStr` field, or ``None``."""
    return None if value is None else value.get_secret_value()


class StoreConfig(BaseModel):
    """URLs / credentials for the optional stores; every field defaults to off.

    Build from the environment with :meth:`from_env`. A ``None`` field means the
    corresponding feature is disabled and the caller should no-op rather than
    fail — the whole point of the layer being optional.

    ``pg_url`` (which usually carries a password) and the S3 keys are
    :class:`~pydantic.SecretStr`, so the model's repr, a log line or a
    ``model_dump()`` shows ``**********``; :func:`secret_value` unwraps one where
    a client is built.
    """

    pg_url: SecretStr | None = None
    pg_pool_min_size: int = 0
    pg_pool_max_size: int = 8
    qdrant_url: str | None = None
    s3_endpoint: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_bucket: str | None = None
    s3_region: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StoreConfig:
        """Read ``CORTEX_*`` variables from *env* (defaults to the process environment with the suite file behind
        it, :data:`argus_cortex.settings.environ`).

        An empty string is treated as unset, so ``CORTEX_PG_URL=`` in a ``.env``
        disables the feature just like omitting it.
        """
        env = settings.environ if env is None else env

        def get(key: str) -> str | None:
            return env.get(key) or None

        def get_int(key: str, default: int) -> int:
            value = env.get(key)
            if not value:
                return default
            try:
                return int(value)
            except ValueError:
                # A non-numeric value falls back to the default rather than
                # crashing from_env() (which every store reads, even the null one).
                return default

        return cls(
            pg_url=get("CORTEX_PG_URL"),
            pg_pool_min_size=get_int("CORTEX_PG_POOL_MIN_SIZE", 0),
            pg_pool_max_size=get_int("CORTEX_PG_POOL_MAX_SIZE", 8),
            qdrant_url=get("CORTEX_QDRANT_URL"),
            s3_endpoint=get("CORTEX_S3_ENDPOINT"),
            s3_access_key=get("CORTEX_S3_ACCESS_KEY"),
            s3_secret_key=get("CORTEX_S3_SECRET_KEY"),
            s3_bucket=get("CORTEX_S3_BUCKET"),
            s3_region=get("CORTEX_S3_REGION"),
        )
