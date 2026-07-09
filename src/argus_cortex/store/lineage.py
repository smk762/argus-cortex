"""The lineage store — persist the caption→edit→dataset→run DAG in Postgres.

Phase 1 of the stateful services layer (see the tracking issue). It is
**optional and degrades to a no-op**: :func:`open_lineage_store` returns a
:class:`NullLineageStore` when no ``CORTEX_PG_URL`` is configured, so a caller
writes the same ``store.record_caption(...)`` code whether or not persistence is
switched on. When a URL *is* set, :class:`PostgresLineageStore` talks to Postgres
via psycopg, which lives behind the ``argus-cortex[postgres]`` extra and is
imported lazily (mirroring how ``backends`` treats httpx).

Everything here is synchronous, matching the rest of cortex. Writes go through a
connection **pool**, so from an async caller you can fan out
``asyncio.to_thread(store.record_*, …)`` concurrently — each call borrows its own
connection — and a dropped connection is replaced transparently. Wrap a
multi-step write in :meth:`PostgresLineageStore.transaction` to make it atomic.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from argus_cortex.store.config import StoreConfig
from argus_cortex.store.errors import StoreError, require_extra, resolve_error_types, wrap_errors
from argus_cortex.store.schema import SCHEMA_STATEMENTS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from argus_cortex.store.models import Caption, HumanEdit, SourceAsset, TrainingRun

# The connection bound to the current transaction() block (per-context, so it
# survives asyncio.to_thread); None means "borrow a fresh one from the pool".
_ACTIVE_CONN: ContextVar[Any] = ContextVar("_argus_lineage_conn", default=None)

__all__ = [
    "StoreError",
    "LineageStore",
    "NullLineageStore",
    "PostgresLineageStore",
    "open_lineage_store",
]


@runtime_checkable
class LineageStore(Protocol):
    """The persistence contract every lineage backend implements.

    The recording methods return the new row's id (a ``str``), or ``None`` when
    persistence is disabled — so a caller can thread ids through the DAG without
    branching on whether a store is live.
    """

    @property
    def is_enabled(self) -> bool:
        """Whether writes are actually persisted (``False`` for the null store)."""
        ...

    def ensure_schema(self) -> None:
        """Create the lineage tables if they don't exist (idempotent)."""
        ...

    def record_asset(self, asset: SourceAsset) -> str | None:
        """Insert (or reuse, by ``sha256``) a source asset; return its id."""
        ...

    def record_caption(self, asset_id: str, caption: Caption) -> str | None:
        """Insert a caption of *asset_id*; return its id."""
        ...

    def record_edit(self, caption_id: str, edit: HumanEdit) -> str | None:
        """Insert a human edit of *caption_id*; return its id."""
        ...

    def record_training_run(self, run: TrainingRun) -> str | None:
        """Insert a training run; return its id."""
        ...

    def add_to_dataset(
        self,
        dataset: str,
        caption_id: str,
        *,
        edit_id: str | None = None,
        training_run_id: str | None = None,
    ) -> str | None:
        """Record that *caption_id* belongs to *dataset*; return the membership id."""
        ...

    def caption_edit_pairs(self, dataset: str | None = None) -> list[tuple[str, str]]:
        """Return ``(model_caption, edited_caption)`` pairs for the feedback loop."""
        ...

    def transaction(self) -> AbstractContextManager[None]:
        """Run the enclosed writes as one atomic unit (a no-op on the null store)."""
        ...

    def close(self) -> None:
        """Release the connection pool."""
        ...


class NullLineageStore:
    """No-op store used when no database is configured — the graceful degrade.

    Every write returns ``None`` and reads return empty, so the feature simply
    does nothing when ``CORTEX_PG_URL`` is unset instead of forcing callers to
    guard every call.
    """

    is_enabled = False

    def ensure_schema(self) -> None:
        return None

    def record_asset(self, asset: SourceAsset) -> str | None:
        return None

    def record_caption(self, asset_id: str, caption: Caption) -> str | None:
        return None

    def record_edit(self, caption_id: str, edit: HumanEdit) -> str | None:
        return None

    def record_training_run(self, run: TrainingRun) -> str | None:
        return None

    def add_to_dataset(
        self,
        dataset: str,
        caption_id: str,
        *,
        edit_id: str | None = None,
        training_run_id: str | None = None,
    ) -> str | None:
        return None

    def caption_edit_pairs(self, dataset: str | None = None) -> list[tuple[str, str]]:
        return []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def close(self) -> None:
        return None


class PostgresLineageStore:
    """Persist the lineage DAG to PostgreSQL via a psycopg connection pool.

    Point it at a database by DSN (``postgresql://…``). A
    ``psycopg_pool.ConnectionPool`` is created lazily on first use
    (``argus-cortex[postgres]``) and hands out an autocommit connection per
    operation, so concurrent calls (e.g. several ``asyncio.to_thread(store.record_*,
    …)``) are safe and a dropped connection (Postgres restart / idle timeout) is
    transparently replaced — there's no single shared connection to brick. An
    injected ``pool`` (tests) bypasses the driver and the network. jsonb columns
    are sent as text with a ``::jsonb`` cast so no driver-specific adapters are
    needed at call sites.
    """

    is_enabled = True

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8, pool: Any = None) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self._pool = pool  # injectable pool (tests), or None -> lazily created
        self._lock = threading.Lock()

    # -- connection / execution ------------------------------------------------

    def _get_pool(self) -> Any:
        # Double-checked locking so a concurrent first call can't open two pools.
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    pool_mod = require_extra("psycopg_pool", "postgres", feature="postgres lineage")
                    self._pool = pool_mod.ConnectionPool(
                        self.dsn,
                        min_size=self.min_size,
                        max_size=self.max_size,
                        kwargs={"autocommit": True},
                        # Validate a connection on borrow so one gone stale (Postgres
                        # restart / idle timeout) is discarded and replaced, not handed
                        # out dead — this is what makes reconnection transparent.
                        check=pool_mod.ConnectionPool.check_connection,
                        open=True,
                    )
        return self._pool

    @contextmanager
    def _acquire(self) -> Iterator[Any]:
        # Inside transaction(), reuse the ambient connection; otherwise borrow one
        # from the pool for a single op and return it (the pool owns concurrency
        # and reconnection).
        conn = _ACTIVE_CONN.get()
        if conn is not None:
            yield conn
        else:
            with self._get_pool().connection() as conn:
                yield conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the enclosed ``record_*`` / ``add_to_dataset`` calls atomically.

        Every write in the block shares one pooled connection and commits together
        on a clean exit, or rolls back as a unit on error — so a torn DAG (an asset
        with no caption, a caption in no dataset) is never committed. Use it from a
        single thread (e.g. ``await asyncio.to_thread(do_writes)``); nested calls
        become savepoints within the outer transaction.
        """
        existing = _ACTIVE_CONN.get()
        if existing is not None:
            with existing.transaction():  # nested -> savepoint within the outer tx
                yield
            return
        try:
            with self._get_pool().connection() as conn, conn.transaction():
                token = _ACTIVE_CONN.set(conn)
                try:
                    yield
                finally:
                    _ACTIVE_CONN.reset(token)
        except self._db_errors() as exc:
            # Pool acquisition / commit failures become StoreError. An inner write's
            # StoreError is a RuntimeError, not a psycopg error, so it isn't caught
            # here — it propagates as-is (after the block rolls back) rather than
            # being double-wrapped.
            raise StoreError(f"transaction failed: {exc}") from exc

    @staticmethod
    def _db_errors() -> tuple[type[BaseException], ...]:
        """psycopg's error hierarchy, so operational failures fold into StoreError.

        Every psycopg exception (including ``psycopg_pool`` timeouts) derives from
        ``psycopg.Error``; without the driver installed there is nothing to wrap
        (the missing-driver path raises StoreError in :meth:`_get_pool`).
        """
        return resolve_error_types((("psycopg", "Error"),))

    def _insert(self, sql: str, params: tuple[Any, ...]) -> str | None:
        """Run an ``INSERT … RETURNING id`` and return the id as a string."""

        def op() -> str | None:
            with self._acquire() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            return str(row[0]) if row and row[0] is not None else None

        return wrap_errors(op, errors=self._db_errors(), label="insert")

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        def op() -> list[tuple[Any, ...]]:
            with self._acquire() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

        return wrap_errors(op, errors=self._db_errors(), label="query")

    @staticmethod
    def _json(value: Any) -> str:
        # default=str so a datetime / UUID / Path / Decimal in a metadata bag
        # serialises instead of raising TypeError mid-write.
        return json.dumps(value or {}, default=str)

    # -- schema ----------------------------------------------------------------

    def ensure_schema(self) -> None:
        def op() -> None:
            with self._acquire() as conn, conn.cursor() as cur:
                for statement in SCHEMA_STATEMENTS:
                    cur.execute(statement)

        wrap_errors(op, errors=self._db_errors(), label="ensure_schema")

    # -- writes ----------------------------------------------------------------

    def record_asset(self, asset: SourceAsset) -> str | None:
        # De-dupe on content hash: re-ingesting identical bytes returns the
        # existing row (DO UPDATE ensures RETURNING fires even on conflict) and
        # refreshes uri/immich_id/metadata to the latest call (last-write-wins).
        # NOTE: dedup only works when sha256 is set — NULLs are distinct under the
        # UNIQUE index, so a hash-less asset inserts a fresh row every call.
        return self._insert(
            """
            INSERT INTO source_asset (uri, sha256, immich_id, metadata)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (sha256) DO UPDATE
                SET uri = EXCLUDED.uri,
                    immich_id = EXCLUDED.immich_id,
                    metadata = EXCLUDED.metadata
            RETURNING id
            """,
            (asset.uri, asset.sha256, asset.immich_id, self._json(asset.metadata)),
        )

    def record_caption(self, asset_id: str, caption: Caption) -> str | None:
        return self._insert(
            """
            INSERT INTO caption
                (asset_id, version, backend, profile, params,
                 final_caption, variants, raw_tags, raw_prose, metadata)
            VALUES (%s::uuid, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                asset_id,
                caption.version,
                caption.backend,
                self._json(caption.profile),
                self._json(caption.params),
                caption.final_caption,
                self._json(caption.variants),
                caption.raw_tags,
                caption.raw_prose,
                self._json(caption.metadata),
            ),
        )

    def record_edit(self, caption_id: str, edit: HumanEdit) -> str | None:
        return self._insert(
            """
            INSERT INTO human_edit (caption_id, edited_caption, editor, note)
            VALUES (%s::uuid, %s, %s, %s)
            RETURNING id
            """,
            (caption_id, edit.edited_caption, edit.editor, edit.note),
        )

    def record_training_run(self, run: TrainingRun) -> str | None:
        return self._insert(
            """
            INSERT INTO training_run (dataset, base_model, params)
            VALUES (%s, %s, %s::jsonb)
            RETURNING id
            """,
            (run.dataset, run.base_model, self._json(run.params)),
        )

    def add_to_dataset(
        self,
        dataset: str,
        caption_id: str,
        *,
        edit_id: str | None = None,
        training_run_id: str | None = None,
    ) -> str | None:
        # Idempotent per (dataset, caption): re-adding sets any link passed and
        # PRESERVES a previously-stored one when the arg is omitted (COALESCE),
        # so attaching the training_run later doesn't wipe the earlier edit_id.
        return self._insert(
            """
            INSERT INTO dataset_membership (dataset, caption_id, edit_id, training_run_id)
            VALUES (%s, %s::uuid, %s::uuid, %s::uuid)
            ON CONFLICT (dataset, caption_id) DO UPDATE
                SET edit_id = COALESCE(EXCLUDED.edit_id, dataset_membership.edit_id),
                    training_run_id = COALESCE(EXCLUDED.training_run_id, dataset_membership.training_run_id)
            RETURNING id
            """,
            (dataset, caption_id, edit_id, training_run_id),
        )

    # -- reads -----------------------------------------------------------------

    def caption_edit_pairs(self, dataset: str | None = None) -> list[tuple[str, str]]:
        """``(model_caption, edited_caption)`` pairs, optionally scoped to a dataset.

        The training signal for the reconciliation summariser: what the model
        wrote vs. what a human corrected it to.

        Unscoped, every human edit contributes a pair. Scoped to a *dataset*, only
        the edit that dataset actually selected (``dataset_membership.edit_id``) is
        returned — the join is on that specific edit, so superseded edits and
        member captions with no pinned edit are excluded rather than polluting the
        signal.
        """
        # One base query; the dataset scope adds a join+filter on the *selected*
        # edit so the two forms can't drift in projection/ordering.
        base = """
            SELECT c.final_caption, e.edited_caption
            FROM human_edit e
            JOIN caption c ON c.id = e.caption_id
        """
        if dataset is None:
            sql = base + " ORDER BY e.created_at"
            params: tuple[Any, ...] = ()
        else:
            sql = (
                base
                + " JOIN dataset_membership m ON m.caption_id = c.id AND m.edit_id = e.id"
                + " WHERE m.dataset = %s ORDER BY e.created_at"
            )
            params = (dataset,)
        rows = self._fetchall(sql, params)
        return [(str(a), str(b)) for a, b in rows]

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None


def open_lineage_store(config: StoreConfig | None = None) -> LineageStore:
    """Return a live :class:`PostgresLineageStore`, or a :class:`NullLineageStore`.

    The single entry point callers use. With no *config*, reads
    :meth:`StoreConfig.from_env <argus_cortex.store.config.StoreConfig.from_env>`;
    when ``pg_url`` is unset the returned store no-ops, so persistence is opt-in
    by simply setting ``CORTEX_PG_URL``.
    """
    if config is None:
        config = StoreConfig.from_env()
    if config.pg_url:
        return PostgresLineageStore(
            config.pg_url,
            min_size=config.pg_pool_min_size,
            max_size=config.pg_pool_max_size,
        )
    return NullLineageStore()
