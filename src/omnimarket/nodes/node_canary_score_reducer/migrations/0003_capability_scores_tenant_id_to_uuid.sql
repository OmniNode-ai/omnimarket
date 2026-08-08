-- OMN-15356: convert capability_scores.tenant_id from the legacy TEXT slug
-- to the canonical UUID identity, for the classified-TENANT relation set.
--
-- OMN-15732 AC2 ADJUDICATION (2026-08-08, no gate widened)
--   An earlier revision of this migration split the mapping into a
--   standalone, schema-qualified `platform_catalog.house_tenant_map_slug_to_
--   uuid` function so it could be ownership-declared as a new application
--   object. That placement was rejected on adjudication: the migration-ledger
--   `domain` column is the *declared target schema domain* (omnibase_infra
--   docker/migrations/forward/_ledger/bootstrap.sql), and `platform_catalog`
--   (topology domain PLATFORM_CATALOG) is inadmissible for a `node:%`
--   migration stream at both the static gate
--   (scripts/validation/validate_application_migration_manifest.py) and the
--   runtime `schema_migrations_stream_domain_check` CHECK constraint, which
--   together restrict node-stream domains to exactly {tenant,
--   omninode_internal}. `tenant` and `omninode_internal` both fail
--   apply-time schema presence today (the former needs the still-pending
--   OMN-15359 cutover; the latter is created only by the cutover-only
--   initializer, never by the forward-migration stream). The function has
--   exactly one consumer -- this file's own DDL-time `USING` clause -- and no
--   runtime caller; the runtime implementation is the single Python
--   function `omnimarket.projection.tenant_isolation.resolve_tenant_uuid`.
--   A persistent catalog object read once by one ALTER buys nothing here and
--   cannot be given a truthful ownership/domain declaration on every surface
--   at once. The mapping is therefore inlined below with no CREATE FUNCTION
--   and no new schema-qualified object; OMN-15359's governed tenant/internal
--   cutover is the right place to promote this into a shared,
--   truthfully-declared `tenant.house_tenant_map_slug_to_uuid` alongside the
--   other classified-TENANT relations' conversions.
--
-- WHY THIS TABLE FIRST
--   capability_scores is the relation the OMN-15356 identity module
--   (`omnimarket.projection.tenant_isolation.resolve_tenant_uuid`) and its
--   test (`test_the_tenant_classified_relations_carry_a_tenant_id_migration`)
--   already use as the canonical worked example. This migration lands the
--   full end-to-end pattern -- inline fail-closed mapping, conversion,
--   index/constraint preservation, RLS policy cast -- for ONE relation as the
--   reviewable shape. The remaining classified-TENANT relations (see the
--   `expected` table in `test_house_tenant_identity.py` plus
--   delegation_events/delegation_judge_verdict_events/delegation_budget_state/
--   inference_response_text/savings_estimates/node_service_registry) convert
--   via the identical mechanical pattern in follow-up migrations under this
--   same ticket -- NOT silently dropped, deferred and named in the PR body.
--
-- FAIL-CLOSED: NO SENTINEL SURVIVES (two independent guards)
--   (1) A pre-guard `IF EXISTS` check below RAISEs before any DDL runs if any
--   row's tenant_id is not the one closed mapping key this codebase knows
--   about. (2) Even if that guard were ever bypassed, the `ALTER COLUMN ...
--   TYPE UUID USING (CASE ... END)` below has no ELSE branch: an unmapped
--   value evaluates the CASE to NULL, which then violates the column's
--   pre-existing NOT NULL constraint (migration 0002) and aborts the
--   statement. Postgres evaluates the `USING` expression for every row as
--   part of the single `ALTER TABLE` DDL statement, and because DDL in
--   PostgreSQL is transactional, either guard failing rolls back the entire
--   migration transaction. There is no partial conversion, no invented UUID,
--   and no 'unmapped rows keep their old value' fallback: the column stays
--   TEXT until every row maps, exactly the "no sentinel/default survives"
--   acceptance criterion.
--
-- CONSTRAINTS AND INDEXES SURVIVE THE TYPE CHANGE
--   `ALTER COLUMN ... TYPE` rewrites the column in place; the pre-existing
--   `idx_capability_scores_tenant_id` index and the
--   `capability_scores_model_key_task_type_key` UNIQUE constraint (on
--   unrelated columns, untouched here) are NOT dropped and NOT recreated by
--   this migration -- PostgreSQL preserves an index across a column type
--   change automatically when the new type has a binary-compatible or
--   USING-expressed conversion path, which this is. The Docker proof in
--   omnibase_infra (docker/tenant-uuid-conversion-proof) asserts this
--   directly rather than assuming it.
--
-- RLS POLICY: THE GUC COMPARISON GAINS AN EXPLICIT CAST
--   `current_setting('app.tenant_id', true)` always returns TEXT (GUCs have
--   no native UUID type); the policy predicate must cast it explicitly now
--   that the column itself is UUID. `::uuid` on a non-UUID-shaped GUC value
--   raises rather than silently coercing, so an unset or malformed GUC still
--   fails closed -- proving that behavior is OMN-15416's scope (real
--   non-owner pools), not duplicated here.
--
-- Idempotent: the column-type guard only runs the ALTER when the column is
-- not already UUID, so a second application is a no-op.

DO $$
DECLARE
    v_current_type TEXT;
BEGIN
    SELECT atttypid::regtype::text INTO v_current_type
    FROM pg_attribute
    WHERE attrelid = 'public.capability_scores'::regclass
      AND attname = 'tenant_id'
      AND NOT attisdropped;

    IF v_current_type IS NULL THEN
        RAISE EXCEPTION
            'OMN-15356: public.capability_scores.tenant_id column not found -- '
            'expected migration 0002 to have already landed it';
    ELSIF v_current_type = 'uuid' THEN
        RAISE NOTICE
            'public.capability_scores.tenant_id is already uuid; skipping conversion';
    ELSIF v_current_type = 'text' THEN
        -- Pre-guard: refuse before any DDL runs if any row carries a
        -- tenant_id value outside the closed mapping (today: 'omninode'
        -- only). This is the first of the two independent fail-closed
        -- guards described in the file header above.
        IF EXISTS (
            SELECT 1 FROM public.capability_scores
            WHERE tenant_id IS DISTINCT FROM 'omninode'
        ) THEN
            RAISE EXCEPTION
                'OMN-15356: no canonical UUID mapping for tenant value % -- '
                'refusing to invent or default one; extend the closed mapping '
                'in this migration only after confirming this is a real, '
                'reviewed tenant identity',
                (
                    SELECT tenant_id FROM public.capability_scores
                    WHERE tenant_id IS DISTINCT FROM 'omninode'
                    LIMIT 1
                );
        END IF;

        -- The pre-existing tenant_isolation POLICY (migration 0002) depends
        -- on this column -- PostgreSQL refuses ALTER COLUMN ... TYPE while
        -- any policy references it, so the policy must be dropped first and
        -- recreated (with the ::uuid cast) after the type change, not just
        -- before/after this DO block. Caught by the Docker fixture proof
        -- (docker/tenant-uuid-conversion-proof) before this ever reached a
        -- real database.
        DROP POLICY IF EXISTS tenant_isolation ON public.capability_scores;

        -- The pre-existing TEXT DEFAULT ('omninode') is not automatically
        -- castable to uuid -- Postgres tries to cast the DEFAULT expression
        -- itself (not just the stored rows) when the column type changes,
        -- and 'omninode'::uuid is not a valid uuid literal, so the DEFAULT
        -- must be dropped before the TYPE change and a new uuid-typed
        -- DEFAULT set after it. Also caught by the same fixture proof.
        ALTER TABLE public.capability_scores
            ALTER COLUMN tenant_id DROP DEFAULT;
        -- Second, independent fail-closed guard: the CASE has no ELSE, so
        -- any value that reaches this point despite the pre-guard above
        -- (e.g. a row inserted between the guard and the ALTER within the
        -- same transaction) evaluates to NULL and is rejected by the
        -- column's existing NOT NULL constraint, aborting the statement.
        ALTER TABLE public.capability_scores
            ALTER COLUMN tenant_id TYPE UUID
            USING (
                CASE tenant_id
                    WHEN 'omninode' THEN '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid
                END
            );
        ALTER TABLE public.capability_scores
            ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;
    ELSE
        RAISE EXCEPTION
            'OMN-15356: public.capability_scores.tenant_id has unexpected type %, '
            'expected text or uuid -- operator schema ruling required',
            v_current_type;
    END IF;
END$$;

-- Idempotent whether or not the DO block above dropped it (the 'uuid'
-- branch above leaves the existing (already-cast) policy in place, so this
-- DROP + CREATE additionally covers a hand-repaired or partially-applied
-- prior state without erroring).
DROP POLICY IF EXISTS tenant_isolation ON public.capability_scores;
CREATE POLICY tenant_isolation ON public.capability_scores
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- OMN-14894 ratchet: every file that (re)creates this policy must grant
-- app_dashboard SELECT in the same file. Idempotent; already granted by
-- migration 0002, restated here so this file alone satisfies the ratchet.
GRANT SELECT ON public.capability_scores TO app_dashboard;
