from __future__ import annotations

from argus_cortex.store import (
    Caption,
    HumanEdit,
    LineageStore,
    NullLineageStore,
    PostgresLineageStore,
    SourceAsset,
    StoreConfig,
    TrainingRun,
    open_lineage_store,
)
from argus_cortex.store.schema import SCHEMA_STATEMENTS

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_store_config_from_env_reads_cortex_vars() -> None:
    cfg = StoreConfig.from_env(
        {
            "CORTEX_PG_URL": "postgresql://localhost/argus",
            "CORTEX_QDRANT_URL": "http://localhost:6333",
            "CORTEX_S3_BUCKET": "argus",
        }
    )
    assert cfg.pg_url == "postgresql://localhost/argus"
    assert cfg.qdrant_url == "http://localhost:6333"
    assert cfg.s3_bucket == "argus"
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

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed.append((sql, params))

    def fetchone(self) -> tuple:
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
    # metadata is serialised to a JSON string for the ::jsonb cast
    assert params == ("immich://1", "abc", None, '{"k": "v"}')


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


def test_record_edit_and_training_run_and_membership() -> None:
    conn = _FakeConn(next_id="x")
    store = _store_with(conn)
    assert store.record_edit("cap-7", HumanEdit(edited_caption="a smiling person", editor="alice")) == "x"
    assert store.record_training_run(TrainingRun(dataset="lora-a", base_model="sdxl")) == "x"
    assert store.add_to_dataset("lora-a", "cap-7", edit_id="e-1") == "x"
    membership_sql = conn.executed[-1][0]
    assert "INSERT INTO dataset_membership" in membership_sql
    assert "ON CONFLICT (dataset, caption_id)" in membership_sql


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
