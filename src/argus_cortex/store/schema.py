"""The lineage DAG schema (Phase 1: Postgres).

Statements are ``CREATE TABLE IF NOT EXISTS`` so :meth:`ensure_schema
<argus_cortex.store.lineage.PostgresLineageStore.ensure_schema>` is idempotent —
this is a lightweight bootstrap, not a migration framework. When the schema needs
to *evolve* (rename/alter columns), introduce a real migration tool; adding new
tables/columns here idempotently is fine until then.

``gen_random_uuid()`` is a core function in PostgreSQL 13+. On older servers,
``CREATE EXTENSION pgcrypto`` provides it.
"""

from __future__ import annotations

# Individual statements (not one blob): psycopg's extended protocol runs one
# command per execute(), and looping keeps ensure_schema() driver-agnostic.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS source_asset (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        uri        text NOT NULL,
        sha256     text UNIQUE,
        immich_id  text,
        metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS caption (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        asset_id      uuid NOT NULL REFERENCES source_asset(id) ON DELETE CASCADE,
        version       integer NOT NULL DEFAULT 1,
        backend       text,
        profile       jsonb NOT NULL DEFAULT '{}'::jsonb,
        params        jsonb NOT NULL DEFAULT '{}'::jsonb,
        final_caption text NOT NULL,
        variants      jsonb NOT NULL DEFAULT '{}'::jsonb,
        raw_tags      text,
        raw_prose     text,
        metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS caption_asset_id_idx ON caption (asset_id)",
    """
    CREATE TABLE IF NOT EXISTS human_edit (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        caption_id     uuid NOT NULL REFERENCES caption(id) ON DELETE CASCADE,
        edited_caption text NOT NULL,
        editor         text,
        note           text,
        created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS human_edit_caption_id_idx ON human_edit (caption_id)",
    """
    CREATE TABLE IF NOT EXISTS training_run (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        dataset    text NOT NULL,
        base_model text,
        params     jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Membership links a caption (and optionally the edit that superseded it) to a
    # named dataset and, once trained, to the run that consumed it.
    """
    CREATE TABLE IF NOT EXISTS dataset_membership (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        dataset         text NOT NULL,
        caption_id      uuid NOT NULL REFERENCES caption(id) ON DELETE CASCADE,
        edit_id         uuid REFERENCES human_edit(id) ON DELETE SET NULL,
        training_run_id uuid REFERENCES training_run(id) ON DELETE SET NULL,
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (dataset, caption_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS dataset_membership_dataset_idx ON dataset_membership (dataset)",
)
