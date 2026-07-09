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

from argus_cortex.store import Caption, HumanEdit, PostgresLineageStore, SourceAsset

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


def test_concurrent_writes_are_isolated(store: PostgresLineageStore) -> None:
    # Many concurrent record_asset calls each borrow their own pooled connection;
    # all succeed with distinct ids (no "another operation in progress").
    def make(i: int) -> str | None:
        return store.record_asset(SourceAsset(uri=f"conc://{i}", sha256=uuid.uuid4().hex))

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        ids = list(ex.map(make, range(32)))
    assert len(set(ids)) == 32
    assert all(i is not None for i in ids)
