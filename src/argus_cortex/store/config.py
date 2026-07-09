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

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Mapping


class StoreConfig(BaseModel):
    """URLs / credentials for the optional stores; every field defaults to off.

    Build from the environment with :meth:`from_env`. A ``None`` field means the
    corresponding feature is disabled and the caller should no-op rather than
    fail — the whole point of the layer being optional.
    """

    pg_url: str | None = None
    qdrant_url: str | None = None
    s3_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StoreConfig:
        """Read ``CORTEX_*`` variables from *env* (defaults to ``os.environ``).

        An empty string is treated as unset, so ``CORTEX_PG_URL=`` in a ``.env``
        disables the feature just like omitting it.
        """
        env = os.environ if env is None else env

        def get(key: str) -> str | None:
            return env.get(key) or None

        return cls(
            pg_url=get("CORTEX_PG_URL"),
            qdrant_url=get("CORTEX_QDRANT_URL"),
            s3_endpoint=get("CORTEX_S3_ENDPOINT"),
            s3_access_key=get("CORTEX_S3_ACCESS_KEY"),
            s3_secret_key=get("CORTEX_S3_SECRET_KEY"),
            s3_bucket=get("CORTEX_S3_BUCKET"),
        )
