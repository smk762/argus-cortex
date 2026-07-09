from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import pytest

from argus_cortex.store import (
    Caption,
    HumanEdit,
    LineageStore,
    NullLineageStore,
    PostgresLineageStore,
    SourceAsset,
    StoreConfig,
    StoreError,
    TrainingRun,
    open_lineage_store,
)
from argus_cortex.store.schema import SCHEMA_STATEMENTS

_HAS_PSYCOPG = importlib.util.find_spec("psycopg") is not None
_HAS_PSYCOPG_POOL = importlib.util.find_spec("psycopg_pool") is not None

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_store_config_from_env_reads_cortex_vars() -> None:
    cfg = StoreConfig.from_env(
        {
            "CORTEX_PG_URL": "postgresql://localhost/argus",
            "CORTEX_QDRANT_URL": "http://localhost:6333",
            "CORTEX_S3_BUCKET": "argus",
            "CORTEX_S3_REGION": "eu-west-1",
        }
    )
    assert cfg.pg_url == "postgresql://localhost/argus"
    assert cfg.qdrant_url == "http://localhost:6333"
    assert cfg.s3_bucket == "argus"
    assert cfg.s3_region == "eu-west-1"
    assert cfg.s3_endpoint is None
    # pool sizes default when unset
    assert cfg.pg_pool_min_size == 0
    assert cfg.pg_pool_max_size == 8


def test_store_config_reads_pool_sizes() -> None:
    cfg = StoreConfig.from_env({"CORTEX_PG_POOL_MIN_SIZE": "2", "CORTEX_PG_POOL_MAX_SIZE": "20"})
    assert cfg.pg_pool_min_size == 2
    assert cfg.pg_pool_max_size == 20


def test_store_config_non_numeric_pool_size_falls_back_to_default() -> None:
    # a typo'd value must not crash from_env(); it falls back to the default
    cfg = StoreConfig.from_env({"CORTEX_PG_POOL_MAX_SIZE": "abc"})
    assert cfg.pg_pool_max_size == 8


def test_store_config_empty_string_is_unset() -> None:
    # `CORTEX_PG_URL=` in a .env disables the feature just like omitting it.
    cfg = StoreConfig.from_env({"CORTEX_PG_URL": ""})
    assert cfg.pg_url is None


# --------------------------------------------------------------------------
# CaptionResult adapter (duck-typed; no argus-lens dependency)
# --------------------------------------------------------------------------


class _FakeCaptionResult:
    final_caption = "a person standing"
    caption_variants = {"identity": "a person", "training": "standing"}
    raw_tags = "1girl, standing"
    raw_prose = "A person is standing."
    backend_name = "wd14"
    metadata = {"min_score": 0.3}


def test_caption_from_caption_result_maps_fields() -> None:
    cap = Caption.from_caption_result(_FakeCaptionResult(), profile={"target_style": "photo"}, params={"topk": 20})
    assert cap.final_caption == "a person standing"
    assert cap.backend == "wd14"
    assert cap.variants == {"identity": "a person", "training": "standing"}
    assert cap.raw_tags == "1girl, standing"
    assert cap.profile == {"target_style": "photo"}
    assert cap.params == {"topk": 20}
    assert cap.metadata == {"min_score": 0.3}


def test_caption_from_caption_result_explicit_backend_wins() -> None:
    cap = Caption.from_caption_result(_FakeCaptionResult(), backend="florence")
    assert cap.backend == "florence"


# --------------------------------------------------------------------------
# NullLineageStore (graceful degradation)
# --------------------------------------------------------------------------


def test_null_store_noops_and_reports_disabled() -> None:
    store = NullLineageStore()
    assert store.is_enabled is False
    assert store.record_asset(SourceAsset(uri="x")) is None
    assert store.record_caption("aid", Caption(final_caption="c")) is None
    assert store.record_edit("cid", HumanEdit(edited_caption="e")) is None
    assert store.record_training_run(TrainingRun(dataset="d")) is None
    assert store.add_to_dataset("d", "cid") is None
    assert store.caption_edit_pairs() == []
    store.ensure_schema()  # no-op, must not raise
    store.close()


def test_open_lineage_store_no_url_returns_null() -> None:
    store = open_lineage_store(StoreConfig())
    assert isinstance(store, NullLineageStore)
    assert isinstance(store, LineageStore)  # satisfies the runtime-checkable protocol


def test_open_lineage_store_with_url_returns_postgres() -> None:
    store = open_lineage_store(StoreConfig(pg_url="postgresql://localhost/argus"))
    assert isinstance(store, PostgresLineageStore)
    assert store.is_enabled is True


# --------------------------------------------------------------------------
# PostgresLineageStore via an injected fake connection (no real DB / driver)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._last_sql = ""

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed.append((sql, params))
        self._last_sql = sql

    def fetchone(self) -> tuple:
        # Model psycopg: fetchone requires a result-producing statement, so a
        # dropped RETURNING clause fails here instead of silently returning an id.
        if "RETURNING" not in self._last_sql:
            raise RuntimeError("the last operation didn't produce a result")
        return (self._conn.next_id,)

    def fetchall(self) -> list[tuple]:
        return list(self._conn.rows)


class _FakeConn:
    def __init__(self, next_id: str = "id-1", rows: list[tuple] | None = None) -> None:
        self.next_id = next_id
        self.rows = rows or []
        self.executed: list[tuple[str, tuple]] = []
        self.closed = False
        self.tx_entered = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.tx_entered += 1
        yield

    def close(self) -> None:
        self.closed = True


class _FakePool:
    """Stands in for psycopg_pool.ConnectionPool: hands out one connection."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.closed = False
        self.connection_calls = 0

    @contextmanager
    def connection(self) -> Iterator[_FakeConn]:
        self.connection_calls += 1
        yield self._conn

    def close(self) -> None:
        self.closed = True


def _store_with(conn: _FakeConn) -> PostgresLineageStore:
    return PostgresLineageStore("postgresql://localhost/argus", pool=_FakePool(conn))


def test_record_asset_returns_id_and_upserts_on_sha() -> None:
    conn = _FakeConn(next_id="asset-42")
    store = _store_with(conn)
    aid = store.record_asset(SourceAsset(uri="immich://1", sha256="abc", metadata={"k": "v"}))
    assert aid == "asset-42"
    sql, params = conn.executed[-1]
    assert "INSERT INTO source_asset" in sql
    assert "ON CONFLICT (sha256)" in sql
    # on a dedup hit, uri/immich_id/metadata are all refreshed (not just uri)
    assert "immich_id = EXCLUDED.immich_id" in sql
    assert "metadata = EXCLUDED.metadata" in sql
    # metadata is serialised to a JSON string for the ::jsonb cast
    assert params == ("immich://1", "abc", None, '{"k": "v"}')


def test_json_serialises_non_native_metadata_without_raising() -> None:
    # A datetime (or UUID/Path/Decimal) in a metadata bag must not crash the write.
    conn = _FakeConn(next_id="asset-1")
    store = _store_with(conn)
    store.record_asset(SourceAsset(uri="x", metadata={"imported_at": datetime(2026, 7, 8)}))
    _sql, params = conn.executed[-1]
    assert '"imported_at": "2026-07-08 00:00:00"' in params[-1]


def test_record_caption_serialises_json_columns() -> None:
    conn = _FakeConn(next_id="cap-7")
    store = _store_with(conn)
    cid = store.record_caption(
        "asset-42",
        Caption(final_caption="a person", backend="wd14", profile={"target_style": "photo"}),
    )
    assert cid == "cap-7"
    sql, params = conn.executed[-1]
    assert "INSERT INTO caption" in sql
    assert params[0] == "asset-42"
    assert params[2] == "wd14"
    assert '{"target_style": "photo"}' in params  # profile jsonb


def test_record_edit_writes_caption_id_and_text() -> None:
    conn = _FakeConn(next_id="edit-1")
    store = _store_with(conn)
    assert store.record_edit("cap-7", HumanEdit(edited_caption="a smiling person", editor="alice")) == "edit-1"
    sql, params = conn.executed[-1]
    assert "INSERT INTO human_edit" in sql
    assert params == ("cap-7", "a smiling person", "alice", None)


def test_record_training_run_writes_dataset_and_model() -> None:
    conn = _FakeConn(next_id="run-1")
    store = _store_with(conn)
    assert store.record_training_run(TrainingRun(dataset="lora-a", base_model="sdxl")) == "run-1"
    sql, params = conn.executed[-1]
    assert "INSERT INTO training_run" in sql
    assert params[0] == "lora-a"
    assert params[1] == "sdxl"


def test_add_to_dataset_preserves_links_via_coalesce() -> None:
    conn = _FakeConn(next_id="m-1")
    store = _store_with(conn)
    assert store.add_to_dataset("lora-a", "cap-7", edit_id="e-1") == "m-1"
    sql, params = conn.executed[-1]
    assert "INSERT INTO dataset_membership" in sql
    assert "ON CONFLICT (dataset, caption_id)" in sql
    # the passed edit_id reaches the query...
    assert params == ("lora-a", "cap-7", "e-1", None)
    # ...and a re-add with an arg omitted must NOT clobber a stored link to NULL:
    # COALESCE(EXCLUDED.col, existing) keeps the previously-recorded lineage link.
    assert "COALESCE(EXCLUDED.edit_id, dataset_membership.edit_id)" in sql
    assert "COALESCE(EXCLUDED.training_run_id, dataset_membership.training_run_id)" in sql


def test_caption_edit_pairs_all() -> None:
    conn = _FakeConn(rows=[("model wrote this", "human fixed this")])
    store = _store_with(conn)
    pairs = store.caption_edit_pairs()
    assert pairs == [("model wrote this", "human fixed this")]
    assert "JOIN caption c" in conn.executed[-1][0]
    assert "dataset_membership" not in conn.executed[-1][0]  # unscoped query


def test_caption_edit_pairs_scoped_to_dataset() -> None:
    conn = _FakeConn(rows=[("a", "b")])
    store = _store_with(conn)
    pairs = store.caption_edit_pairs(dataset="lora-a")
    assert pairs == [("a", "b")]
    sql, params = conn.executed[-1]
    assert "dataset_membership" in sql
    # must join on the dataset's *selected* edit, not just the caption, so
    # superseded edits don't leak into the feedback-loop training set.
    assert "m.edit_id = e.id" in sql
    assert params == ("lora-a",)


def test_ensure_schema_runs_every_statement() -> None:
    conn = _FakeConn()
    store = _store_with(conn)
    store.ensure_schema()
    assert len(conn.executed) == len(SCHEMA_STATEMENTS)
    assert all("CREATE" in sql for sql, _ in conn.executed)


def test_close_closes_pool() -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)
    store = PostgresLineageStore("postgresql://localhost/argus", pool=pool)
    store.ensure_schema()  # touches the pool
    store.close()
    assert pool.closed is True
    assert store._pool is None


def test_each_op_borrows_from_the_pool() -> None:
    # Without a transaction, every op borrows from the pool (one .connection() per
    # op); the pool — not this store — is what isolates concurrent callers. Real
    # per-connection isolation across threads is covered by the gated integration
    # test (test_store_pg_integration.py).
    conn = _FakeConn()
    pool = _FakePool(conn)
    store = PostgresLineageStore("postgresql://localhost/argus", pool=pool)
    store.record_asset(SourceAsset(uri="a"))
    store.record_asset(SourceAsset(uri="b"))
    assert pool.connection_calls == 2


def test_transaction_is_isolated_per_store() -> None:
    # An ambient transaction on one store must NOT capture another store's writes:
    # storeB borrows its own pooled connection even inside storeA's transaction().
    conn_a, conn_b = _FakeConn(), _FakeConn()
    pool_a, pool_b = _FakePool(conn_a), _FakePool(conn_b)
    store_a = PostgresLineageStore("postgresql://localhost/a", pool=pool_a)
    store_b = PostgresLineageStore("postgresql://localhost/b", pool=pool_b)
    with store_a.transaction():
        store_b.record_asset(SourceAsset(uri="x", sha256="h"))
    assert pool_b.connection_calls == 1  # borrowed its own connection
    assert any("source_asset" in sql for sql, _ in conn_b.executed)
    assert conn_a.executed == []  # storeA's connection ran nothing


def test_nested_transaction_opens_a_savepoint() -> None:
    conn = _FakeConn()
    store = PostgresLineageStore("postgresql://localhost/argus", pool=_FakePool(conn))
    with store.transaction():
        store.record_asset(SourceAsset(uri="a", sha256="h"))
        with store.transaction():  # nested -> savepoint on the same connection
            store.record_caption("aid", Caption(final_caption="c"))
    assert conn.tx_entered == 2  # outer transaction + one savepoint


def test_transaction_shares_one_connection_and_enters_one_tx() -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)
    store = PostgresLineageStore("postgresql://localhost/argus", pool=pool)
    with store.transaction():
        store.record_asset(SourceAsset(uri="a", sha256="h"))
        store.record_caption("aid", Caption(final_caption="c"))
    # one pooled connection + one transaction block for the whole batch,
    # and both writes ran on that connection
    assert pool.connection_calls == 1
    assert conn.tx_entered == 1
    assert sum("INSERT INTO source_asset" in sql for sql, _ in conn.executed) == 1
    assert sum("INSERT INTO caption" in sql for sql, _ in conn.executed) == 1


def test_transaction_propagates_error() -> None:
    # An error inside the block propagates (real conn.transaction() rolls back the
    # batch — actual rollback is asserted in the gated integration test).
    conn = _FakeConn()
    store = PostgresLineageStore("postgresql://localhost/argus", pool=_FakePool(conn))
    with pytest.raises(ValueError, match="boom"), store.transaction():
        store.record_asset(SourceAsset(uri="a", sha256="h"))
        raise ValueError("boom")


def test_invalid_pool_sizes_raise_store_error() -> None:
    with pytest.raises(StoreError, match="invalid pool sizes"):
        PostgresLineageStore("postgresql://localhost/argus", min_size=5, max_size=2)
    with pytest.raises(StoreError, match="invalid pool sizes"):
        PostgresLineageStore("postgresql://localhost/argus", max_size=0)


def test_use_after_close_raises() -> None:
    conn = _FakeConn()
    store = PostgresLineageStore("postgresql://localhost/argus", pool=_FakePool(conn))
    store.ensure_schema()
    store.close()
    # a closed store must refuse to silently rebuild a fresh pool
    with pytest.raises(StoreError, match="closed"):
        store.record_asset(SourceAsset(uri="x"))


@pytest.mark.skipif(not _HAS_PSYCOPG, reason="psycopg not installed; can't construct a psycopg.Error")
def test_operational_failure_wrapped_in_store_error() -> None:
    # A raw psycopg error (dropped connection, pool timeout, constraint violation)
    # must surface as StoreError, the type the docstring tells callers to catch.
    import psycopg

    class _FailPool(_FakePool):
        def connection(self) -> Iterator[_FakeConn]:  # type: ignore[override]
            raise psycopg.OperationalError("connection refused")

    store = PostgresLineageStore("postgresql://localhost/argus", pool=_FailPool(_FakeConn()))
    with pytest.raises(StoreError, match="insert failed"):
        store.record_asset(SourceAsset(uri="x"))


@pytest.mark.skipif(not _HAS_PSYCOPG_POOL, reason="psycopg[pool] not installed")
def test_lazy_pool_is_a_real_connection_pool() -> None:
    import psycopg_pool

    # Unreachable DSN with min_size=0: nothing is opened eagerly (no background
    # connect / warning), so this exercises the real construction path offline.
    store = PostgresLineageStore("postgresql://localhost:1/none", min_size=0, max_size=2)
    try:
        assert isinstance(store._get_pool(), psycopg_pool.ConnectionPool)
    finally:
        store.close()
    assert store._pool is None
