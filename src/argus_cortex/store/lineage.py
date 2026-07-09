"""The lineage store — persist the caption→edit→dataset→run DAG in Postgres.

Phase 1 of the stateful services layer (see the tracking issue). It is
**optional and degrades to a no-op**: :func:`open_lineage_store` returns a
:class:`NullLineageStore` when no ``CORTEX_PG_URL`` is configured, so a caller
writes the same ``store.record_caption(...)`` code whether or not persistence is
switched on. When a URL *is* set, :class:`PostgresLineageStore` talks to Postgres
via psycopg, which lives behind the ``argus-cortex[postgres]`` extra and is
imported lazily (mirroring how ``backends`` treats httpx).

Everything here is synchronous, matching the rest of cortex. From an async
caller (e.g. a FastAPI handler) wrap a call in ``asyncio.to_thread(...)`` so the
event loop isn't blocked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from argus_cortex.store.errors import StoreError, require_extra, wrap_errors
from argus_cortex.store.schema import SCHEMA_STATEMENTS

if TYPE_CHECKING:
    from argus_cortex.store.config import StoreConfig
    from argus_cortex.store.models import Caption, HumanEdit, SourceAsset, TrainingRun

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

    def close(self) -> None:
        """Release the connection."""
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

    def close(self) -> None:
        return None


class PostgresLineageStore:
    """Persist the lineage DAG to PostgreSQL via psycopg.

    Point it at a database by DSN (``postgresql://…``). psycopg is imported
    lazily on first connection (``argus-cortex[postgres]``); an injected ``_conn``
    (tests) bypasses the import and the network entirely. jsonb columns are sent
    as text with a ``::jsonb`` cast so no driver-specific adapters are needed at
    call sites.
    """

    is_enabled = True

    def __init__(self, dsn: str, *, connect: Any = None) -> None:
        self.dsn = dsn
        self._connect = connect  # injectable factory: () -> connection (tests)
        self._conn: Any = None

    # -- connection / execution ------------------------------------------------

    def _connection(self) -> Any:
        if self._conn is None:
            if self._connect is not None:
                self._conn = self._connect()
            else:
                psycopg = require_extra("psycopg", "postgres", feature="postgres lineage")
                self._conn = psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    @staticmethod
    def _db_errors() -> tuple[type[BaseException], ...]:
        """psycopg's error hierarchy, so operational failures fold into StoreError.

        Imported lazily (the driver is optional). Every psycopg exception derives
        from ``psycopg.Error``; without the driver installed there is nothing to
        wrap (the missing-driver path raises StoreError in :meth:`_connection`).
        """
        try:
            import psycopg

            return (psycopg.Error,)
        except ImportError:  # pragma: no cover - no driver means no operational errors to wrap
            return ()

    def _insert(self, sql: str, params: tuple[Any, ...]) -> str | None:
        """Run an ``INSERT … RETURNING id`` and return the id as a string."""

        def op() -> str | None:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            return str(row[0]) if row and row[0] is not None else None

        return wrap_errors(op, errors=self._db_errors(), label="insert")

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        def op() -> list[tuple[Any, ...]]:
            conn = self._connection()
            with conn.cursor() as cur:
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
            conn = self._connection()
            with conn.cursor() as cur:
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
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def open_lineage_store(config: StoreConfig | None = None) -> LineageStore:
    """Return a live :class:`PostgresLineageStore`, or a :class:`NullLineageStore`.

    The single entry point callers use. With no *config*, reads
    :meth:`StoreConfig.from_env <argus_cortex.store.config.StoreConfig.from_env>`;
    when ``pg_url`` is unset the returned store no-ops, so persistence is opt-in
    by simply setting ``CORTEX_PG_URL``.
    """
    if config is None:
        from argus_cortex.store.config import StoreConfig

        config = StoreConfig.from_env()
    if config.pg_url:
        return PostgresLineageStore(config.pg_url)
    return NullLineageStore()
