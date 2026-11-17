"""Integration tests for PostgresLineageStore against a real Postgres.

Skipped unless ``CORTEX_TEST_PG_URL`` points at a reachable database (a throwaway
``postgres:16`` container, or a CI service). These cover what the fake-based unit
tests can't: that non-transaction writes actually commit (autocommit), that
``transaction()`` commits/rolls back atomically for real, and that concurrent
writes are isolated across pooled connections.

    docker run -d -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=argus -p 5432:5432 postgres:16-alpine
    CORTEX_TEST_PG_URL=postgresql://postgres:pw@localhost:5432/argus make test
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import uuid

import pytest

from argus_cortex.store import Caption, HumanEdit, PostgresLineageStore, SourceAsset, StoreError

_PG_URL = os.environ.get("CORTEX_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not _PG_URL, reason="set CORTEX_TEST_PG_URL to run Postgres integration tests")


@pytest.fixture
def store() -> PostgresLineageStore:
    s = PostgresLineageStore(_PG_URL, min_size=1, max_size=8)  # type: ignore[arg-type]
    s.ensure_schema()
    try:
        yield s
    finally:
        s.close()


def _dataset() -> str:
    return f"it-{uuid.uuid4().hex[:12]}"


def test_write_persists_without_an_explicit_transaction(store: PostgresLineageStore) -> None:
    # Proves pooled connections are autocommit: a write is visible from a SEPARATE
    # store instance (a fresh pool/connection), i.e. it really committed.
    dataset = _dataset()
    aid = store.record_asset(SourceAsset(uri=f"it://{dataset}", sha256=uuid.uuid4().hex))
    assert aid is not None
    cid = store.record_caption(aid, Caption(final_caption="model wrote"))
    eid = store.record_edit(cid, HumanEdit(edited_caption="human fixed"))
    store.add_to_dataset(dataset, cid, edit_id=eid)

    other = PostgresLineageStore(_PG_URL, min_size=1, max_size=2)  # type: ignore[arg-type]
    try:
        assert other.caption_edit_pairs(dataset=dataset) == [("model wrote", "human fixed")]
    finally:
        other.close()


def test_membership_refuses_an_edit_of_another_caption(store: PostgresLineageStore) -> None:
    dataset = _dataset()
    aid = store.record_asset(SourceAsset(uri=f"it://{dataset}", sha256=uuid.uuid4().hex))
    mine = store.record_caption(aid, Caption(final_caption="mine"))
    other = store.record_caption(aid, Caption(final_caption="other"))
    foreign_edit = store.record_edit(other, HumanEdit(edited_caption="other's fix"))
    with pytest.raises(StoreError, match="not an edit of caption"):
        store.add_to_dataset(dataset, mine, edit_id=foreign_edit)
    own_edit = store.record_edit(mine, HumanEdit(edited_caption="my fix"))
    store.add_to_dataset(dataset, mine, edit_id=own_edit)
    assert store.caption_edit_pairs(dataset=dataset) == [("mine", "my fix")]


def test_transaction_commits_together(store: PostgresLineageStore) -> None:
    dataset = _dataset()
    with store.transaction():
        aid = store.record_asset(SourceAsset(uri=f"it://{dataset}", sha256=uuid.uuid4().hex))
        cid = store.record_caption(aid, Caption(final_caption="m"))
        eid = store.record_edit(cid, HumanEdit(edited_caption="h"))
        store.add_to_dataset(dataset, cid, edit_id=eid)
    assert store.caption_edit_pairs(dataset=dataset) == [("m", "h")]


def test_transaction_rolls_back_on_error(store: PostgresLineageStore) -> None:
    dataset = _dataset()
    sha = uuid.uuid4().hex
    with pytest.raises(RuntimeError, match="boom"), store.transaction():
        aid = store.record_asset(SourceAsset(uri=f"it://{dataset}", sha256=sha))
        cid = store.record_caption(aid, Caption(final_caption="m"))
        store.add_to_dataset(dataset, cid, edit_id=store.record_edit(cid, HumanEdit(edited_caption="h")))
        raise RuntimeError("boom")
    # nothing from the rolled-back block is visible
    assert store.caption_edit_pairs(dataset=dataset) == []
    # ...and the asset row itself was rolled back too (re-inserting the sha is fine)
    reused = store.record_asset(SourceAsset(uri="fresh", sha256=sha))
    assert reused is not None


def test_reupsert_merges_metadata_and_keeps_immich_id(store: PostgresLineageStore) -> None:
    # quarry records provenance first; lens later records the same bytes with
    # none: the provenance and immich id survive, the uri moves, shared keys update.
    sha = uuid.uuid4().hex
    first = store.record_asset(
        SourceAsset(uri="quarry://a", sha256=sha, immich_id="im-1", metadata={"license": "cc0", "seen": 1})
    )
    again = store.record_asset(SourceAsset(uri="/data/images/a.png", sha256=sha, metadata={"seen": 2}))
    assert again == first
    with store._acquire() as conn:
        row = conn.execute("SELECT uri, immich_id, metadata FROM source_asset WHERE id = %s", (first,)).fetchone()
    assert row == ("/data/images/a.png", "im-1", {"license": "cc0", "seen": 2})


def test_concurrent_writes_are_isolated(store: PostgresLineageStore) -> None:
    # Many concurrent record_asset calls each borrow their own pooled connection;
    # all succeed with distinct ids (no "another operation in progress").
    def make(i: int) -> str | None:
        return store.record_asset(SourceAsset(uri=f"conc://{i}", sha256=uuid.uuid4().hex))

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        ids = list(ex.map(make, range(32)))
    assert len(set(ids)) == 32
    assert all(i is not None for i in ids)


def _schema_lock_is_free() -> bool:
    import psycopg

    from argus_cortex.store.lineage import _SCHEMA_LOCK_KEY

    with psycopg.connect(_PG_URL, autocommit=True) as conn:  # type: ignore[arg-type]
        got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,)).fetchone()[0]
        if got:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_KEY,))
        return bool(got)


def test_ensure_schema_failing_inside_a_transaction_frees_the_lock_and_keeps_its_error(
    store: PostgresLineageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed DDL inside transaction(): the real error surfaces, and no pooled connection keeps the lock."""
    from argus_cortex.store import lineage

    monkeypatch.setattr(lineage, "SCHEMA_STATEMENTS", (*lineage.SCHEMA_STATEMENTS, "SELECT 1 / 0"))
    with pytest.raises(StoreError, match="division by zero"), store.transaction():
        store.ensure_schema()
    assert _schema_lock_is_free()


def test_ensure_schema_in_a_transaction_holds_the_lock_until_it_commits(store: PostgresLineageStore) -> None:
    with store.transaction():
        store.ensure_schema()
        assert not _schema_lock_is_free()  # the DDL isn't committed yet: another stage must still wait
    assert _schema_lock_is_free()


def test_concurrent_ensure_schema_on_a_fresh_database() -> None:
    """Several stages bootstrapping one empty database at once all succeed."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"cortex_it_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(_PG_URL, autocommit=True) as admin:  # type: ignore[arg-type]
        admin.execute(f'CREATE DATABASE "{name}"')
    fresh = make_conninfo(**{**conninfo_to_dict(_PG_URL), "dbname": name})  # type: ignore[arg-type]
    stores = [PostgresLineageStore(fresh, min_size=0, max_size=1) for _ in range(8)]
    try:
        with cf.ThreadPoolExecutor(max_workers=len(stores)) as ex:
            list(ex.map(lambda s: s.ensure_schema(), stores))
        assert stores[0].record_asset(SourceAsset(uri="fresh://1", sha256=uuid.uuid4().hex)) is not None
    finally:
        for s in stores:
            s.close()
        with psycopg.connect(_PG_URL, autocommit=True) as admin:  # type: ignore[arg-type]
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
