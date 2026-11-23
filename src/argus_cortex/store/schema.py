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
    # A membership's edit must be an edit OF its caption, or the exported training
    # text comes from another image. A trigger rather than a composite FK: it runs
    # on PostgreSQL 13+ (a composite FK's ``ON DELETE SET NULL (edit_id)`` needs 15)
    # and reaches tables created before it, which CREATE TABLE IF NOT EXISTS skips.
    # FOR SHARE locks the edit row it checked. The FK's own FOR KEY SHARE does not conflict
    # with an UPDATE of human_edit.caption_id (a non-key column), so without it a concurrent
    # move of the edit and this insert could each pass their check and both commit. With it
    # the two serialise: whichever runs second waits, then sees the other's committed row.
    """
    CREATE OR REPLACE FUNCTION dataset_membership_edit_matches_caption() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.edit_id IS NOT NULL THEN
            PERFORM 1 FROM human_edit WHERE id = NEW.edit_id AND caption_id = NEW.caption_id FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'dataset_membership.edit_id % is not an edit of caption %',
                    NEW.edit_id, NEW.caption_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    # Not DROP + CREATE: a DROP + CREATE pair is not idempotent across the stages
    # that run ensure_schema() at startup. ensure_schema() serialises them behind an
    # advisory lock; tolerating duplicate_object keeps each statement safe on its
    # own too. The logic lives in the function above, which CREATE OR REPLACE updates.
    """
    DO $$
    BEGIN
        CREATE TRIGGER dataset_membership_edit_matches_caption
            BEFORE INSERT OR UPDATE OF edit_id, caption_id ON dataset_membership
            FOR EACH ROW EXECUTE FUNCTION dataset_membership_edit_matches_caption();
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    END
    $$
    """,
    # The same rule from the other side: moving an edit to another caption would
    # leave a membership that names it exporting another image's text.
    """
    CREATE OR REPLACE FUNCTION human_edit_caption_kept_by_memberships() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.caption_id IS DISTINCT FROM OLD.caption_id AND EXISTS (
            SELECT 1 FROM dataset_membership WHERE edit_id = NEW.id AND caption_id <> NEW.caption_id
        ) THEN
            RAISE EXCEPTION 'human_edit % is used by a dataset membership of caption %, so its caption cannot change',
                NEW.id, OLD.caption_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        CREATE TRIGGER human_edit_caption_kept_by_memberships
            BEFORE UPDATE OF caption_id ON human_edit
            FOR EACH ROW EXECUTE FUNCTION human_edit_caption_kept_by_memberships();
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    END
    $$
    """,
)
