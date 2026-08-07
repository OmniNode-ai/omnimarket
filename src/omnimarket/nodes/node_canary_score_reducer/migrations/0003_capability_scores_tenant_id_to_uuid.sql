-- OMN-15356: convert capability_scores.tenant_id from the legacy TEXT slug
-- to the canonical UUID identity, for the classified-TENANT relation set.
--
-- WHY THIS TABLE FIRST
--   capability_scores is the relation the OMN-15356 identity module
--   (`omnimarket.projection.tenant_isolation.resolve_tenant_uuid`) and its
--   test (`test_the_tenant_classified_relations_carry_a_tenant_id_migration`)
--   already use as the canonical worked example. This migration lands the
--   full end-to-end pattern -- mapping function, fail-closed conversion,
--   index/constraint preservation, RLS policy cast -- for ONE relation as the
--   reviewable shape. The remaining classified-TENANT relations (see the
--   `expected` table in `test_house_tenant_identity.py` plus
--   delegation_events/delegation_judge_verdict_events/delegation_budget_state/
--   inference_response_text/savings_estimates/node_service_registry) convert
--   via the identical mechanical pattern in follow-up migrations under this
--   same ticket -- NOT silently dropped, deferred and named in the PR body.
--
-- FAIL-CLOSED: NO SENTINEL SURVIVES
--   `house_tenant_map_slug_to_uuid` is the SQL mirror of
--   `omnimarket.projection.tenant_isolation.resolve_tenant_uuid` -- same
--   closed mapping (today: 'omninode' only), same refusal behavior. Postgres
--   evaluates the `USING` expression for every row as part of the single
--   `ALTER TABLE` DDL statement; if ANY row's tenant_id is not an exact key in
--   the mapping, the function RAISEs, the statement fails, and -- because DDL
--   in PostgreSQL is transactional -- the entire migration transaction rolls
--   back. There is no partial conversion, no invented UUID, and no
--   'unmapped rows keep their old value' fallback: the column stays TEXT
--   until every row maps, exactly the "no sentinel/default survives"
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
-- Idempotent: the mapping function is CREATE OR REPLACE; the column-type
-- guard only runs the ALTER when the column is not already UUID, so a
-- second application is a no-op.

CREATE OR REPLACE FUNCTION house_tenant_map_slug_to_uuid(p_value TEXT)
RETURNS UUID
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF p_value = 'omninode' THEN
        RETURN '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;
    END IF;
    RAISE EXCEPTION
        'OMN-15356: no canonical UUID mapping for tenant value % -- refusing '
        'to invent or default one; extend house_tenant_map_slug_to_uuid only '
        'after confirming this is a real, reviewed tenant identity',
        p_value;
END;
$$;

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
        ALTER TABLE public.capability_scores
            ALTER COLUMN tenant_id TYPE UUID
            USING house_tenant_map_slug_to_uuid(tenant_id);
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
