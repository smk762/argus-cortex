from __future__ import annotations

import importlib.util
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

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def _store_with(conn: _FakeConn) -> PostgresLineageStore:
    return PostgresLineageStore("postgresql://localhost/argus", connect=lambda: conn)


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


def test_close_closes_connection() -> None:
    conn = _FakeConn()
    store = _store_with(conn)
    store.ensure_schema()  # forces a connection
    store.close()
    assert conn.closed is True
    assert store._conn is None


def test_connection_is_reused_across_calls() -> None:
    calls = {"n": 0}

    def connect() -> _FakeConn:
        calls["n"] += 1
        return _FakeConn()

    store = PostgresLineageStore("postgresql://localhost/argus", connect=connect)
    store.record_asset(SourceAsset(uri="a"))
    store.record_asset(SourceAsset(uri="b"))
    assert calls["n"] == 1  # connected once, reused


@pytest.mark.skipif(not _HAS_PSYCOPG, reason="psycopg not installed; can't construct a psycopg.Error")
def test_operational_failure_wrapped_in_store_error() -> None:
    # A raw psycopg error (dropped connection, bad DSN, constraint violation)
    # must surface as StoreError, the type the docstring tells callers to catch.
    import psycopg

    class _Failing(_FakeConn):
        def cursor(self) -> _FakeCursor:  # type: ignore[override]
            raise psycopg.OperationalError("connection refused")

    store = PostgresLineageStore("postgresql://localhost/argus", connect=lambda: _Failing())
    with pytest.raises(StoreError, match="insert failed"):
        store.record_asset(SourceAsset(uri="x"))
