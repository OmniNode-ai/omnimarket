-- OMN-16705: additive re-expression of the identity reconciliation that was
-- previously added by REWRITING 0000 in place.
--
-- WHY THIS FILE EXISTS
--   0000_create_tenant_inference_credentials.sql was edited in place AFTER it
--   had already been applied to a live database:
--
--     559ee461a  2026-08-22 00:19  content sha c1691130...  <- the applied bytes
--                2026-08-22 20:09  applied to the .201 dev lane (omnidash_analytics)
--     7de798a4a  2026-08-24 11:44  content sha d113ac80...  +46 lines of
--                                  reconciliation and five SET NOT NULL
--
--   platform_catalog.schema_migrations on that lane still records c1691130, so
--   _ledger/bootstrap.sql's "conflicting migration checksum in canonical node
--   history" predicate raised on every subsequent forward-migration run and the
--   whole one-shot exited 3 (OMN-16705). Re-stamping the recorded checksum was
--   rejected: the rewritten body carries data reconciliation and constraints
--   that were never applied to that database, so a re-stamp would mark real
--   work done while silently skipping it. Applied migration history is
--   permanent (operator ruling 2026-08-04, OMN-15695): 0000 has been restored
--   to its applied bytes and this file carries the delta forward instead.
--
-- WHAT IT DOES
--   Converges a drifted tenant_inference_credentials table onto the shape 0000
--   declares on its fresh-create path. On the drifted path every column arrives
--   through ALTER TABLE ... ADD COLUMN IF NOT EXISTS, which cannot carry
--   NOT NULL or a PRIMARY KEY, so without this file the fresh and converged
--   paths end at different schemas.
--
--   Rows are repaired before any constraint is tightened, never dropped and
--   never truncated. Every statement is idempotent: the UPDATEs match nothing
--   once they have run, SET NOT NULL is a no-op on an already-NOT NULL column,
--   and the primary key is rebuilt to its declared shape (see the note above
--   that statement for why the rebuild is unconditional).
--
-- name AND provider ARE DELIBERATELY EXCLUDED, and this is load-bearing.
--   The 7de798a4a rewrite set NOT NULL on five columns including name and
--   provider. Re-applying that here would REVERSE
--   0001_relax_name_provider_not_null.sql, which runs BEFORE this file in
--   lexical order and drops exactly those two constraints on purpose: OMN-16324
--   persists a tombstone row (api_key_ref + tenant_id + revoked_at only) when a
--   credential-revoked event overtakes its credential-registered event across
--   two Kafka topics, and such a row has neither a name nor a provider until the
--   register arrives -- which may be never. Filling those columns with
--   placeholder text would also corrupt live tombstones. Net shape is therefore
--   identical to the rewritten 0000 followed by 0001: name and provider
--   nullable, everything else NOT NULL.

WITH missing_api_key_refs AS (
    SELECT
        ctid,
        row_number() OVER (ORDER BY ctid) AS reconciled_rank
    FROM tenant_inference_credentials
    WHERE api_key_ref IS NULL OR api_key_ref = ''
)
UPDATE tenant_inference_credentials AS credentials
SET api_key_ref = '__reconciled_missing_api_key_ref_' || missing_api_key_refs.reconciled_rank::TEXT
FROM missing_api_key_refs
WHERE credentials.ctid = missing_api_key_refs.ctid;

WITH duplicate_api_key_refs AS (
    SELECT
        ctid,
        api_key_ref,
        row_number() OVER (PARTITION BY api_key_ref ORDER BY ctid) AS duplicate_rank,
        row_number() OVER (ORDER BY api_key_ref, ctid) AS global_duplicate_rank
    FROM tenant_inference_credentials
)
UPDATE tenant_inference_credentials AS credentials
SET api_key_ref = credentials.api_key_ref || '__reconciled_duplicate_' || duplicate_api_key_refs.global_duplicate_rank::TEXT
FROM duplicate_api_key_refs
WHERE credentials.ctid = duplicate_api_key_refs.ctid
  AND duplicate_api_key_refs.duplicate_rank > 1;

UPDATE tenant_inference_credentials
SET
    tenant_id = COALESCE(NULLIF(tenant_id, ''), '__reconciled_missing_tenant_' || api_key_ref),
    created_at = COALESCE(created_at, NOW())
WHERE tenant_id IS NULL OR tenant_id = '' OR created_at IS NULL;

ALTER TABLE tenant_inference_credentials
    ALTER COLUMN api_key_ref SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL;

-- Static DROP-then-ADD, NOT a conditional rebuild, and the reason is a gate
-- rather than a preference. Making the rebuild conditional requires inspecting
-- pg_constraint from inside a procedural block, and
-- scripts/ci/check_application_database_sql.py rejects EVERY `DO $$ ... $$`
-- whose relation targets it cannot prove statically -- including a block that
-- uses no dynamic SQL at all. Verified by running that gate against both
-- shapes: the conditional variant fails with "procedural block contains
-- dynamic SQL whose relation targets cannot be proven statically"; this pair
-- passes. The only way to keep the conditional is an exact-path entry in that
-- gate's exemption list, i.e. weakening a required gate to soften an
-- unconditional DDL statement -- which is not a trade this repair will make.
--
-- Cost, stated rather than waved away: ADD CONSTRAINT ... PRIMARY KEY takes an
-- ACCESS EXCLUSIVE lock and rebuilds the backing index, so on a large busy
-- table this pair would block reads and writes. tenant_inference_credentials is
-- a BYOK credential-REF catalog (one short row per registered key, no payloads)
-- and exists on exactly one lane today; the sibling reconciliations in this
-- corpus that are already applied to that same lane -- delegation_routing_
-- tenant_overlay 0001, capability_scores 0001 -- use this identical pair for
-- the same reason. DROP ... IF EXISTS is a no-op when the constraint is absent,
-- and the ADD is safe because the reconciliation above has already made
-- api_key_ref non-null and unique.
ALTER TABLE tenant_inference_credentials
    DROP CONSTRAINT IF EXISTS tenant_inference_credentials_pkey;
ALTER TABLE tenant_inference_credentials
    ADD CONSTRAINT tenant_inference_credentials_pkey PRIMARY KEY (api_key_ref);
