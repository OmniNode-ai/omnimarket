-- OMN-15683 (canonical-key AC of OMN-15356's classified-TENANT conversion
-- sweep): convert delegation_events.tenant_id from the legacy TEXT slug to
-- the canonical UUID identity.
--
-- THE DEFECT THIS CLOSES
--   gateway_workflows.tenant_id (omninode_infra/onex-api, a different
--   database) is UUID. The dashboard's read seam (omnidash auth-middleware +
--   postgres-projection-reader.ts) carries the Keycloak-verified tenant_id
--   claim -- a UUID, per the OMN-16143 Keycloak fix -- into the
--   `app.tenant_id` GUC on every RLS-covered read. With
--   delegation_events.tenant_id still TEXT holding the slug, the RLS
--   predicate `tenant_id = current_setting('app.tenant_id', true)` never
--   matches for a UUID-keyed caller: FORCE RLS returns ZERO ROWS, not an
--   error, so the receipt view renders empty for every real provisioned
--   tenant while the rows are physically present under their slug key. Live
--   probe: 91c74442-1233-4c97-b191-911a10346fdf (beta-business-proof) has 73
--   rows as of 2026-08-18 (grew from 23 at ticket-filing on 2026-08-03); a
--   UUID-keyed read of the same tenant returns 0.
--
-- THE DECISION (OMN-15683 AC1): UUID everywhere, not a mapping relation.
--   This is the same direction OMN-15356 already committed to for every
--   other classified-TENANT relation (capability_scores converted first,
--   migration 0003; delegation_events was explicitly named there as a
--   "remaining classified-TENANT relation" following the identical pattern).
--   A separate mapping relation was considered and rejected: the only
--   existing UUID<->slug mapping (`omninode_cloud.public.tenants`) lives in a
--   DIFFERENT DATABASE from omnidash_analytics, unreachable from this
--   table's RLS predicate without cross-database infrastructure (dblink/FDW)
--   this platform does not run. Converting the column is strictly simpler
--   and matches the sibling relations already converted.
--
-- FAIL-CLOSED: NO SENTINEL SURVIVES (same two independent guards as 0003)
--   (1) A pre-guard `IF EXISTS` check below RAISEs before any DDL runs if any
--   row's tenant_id is outside the closed mapping this migration knows
--   about. (2) The `ALTER COLUMN ... TYPE UUID USING (CASE ... END)` below
--   has no ELSE branch: an unmapped value evaluates to NULL, which violates
--   the column's pre-existing NOT NULL constraint (migration 0022) and
--   aborts the whole transactional DDL statement. There is no partial
--   conversion, no invented UUID, no silent legacy-value passthrough.
--
--   OMN-15683 LIVE FINDING, RESOLVED (2026-08-18): a first probe against
--   onex-dev via the non-superuser `role_omnidash` found the known-candidate
--   sum (omninode=43 + beta-business-proof=73 = 116) did not reconcile
--   against `pg_stat_user_tables.n_live_tup` (124) for this table --
--   `role_omnidash` cannot enumerate the gap itself (RLS blocks a naive
--   GROUP BY for a non-bypassrls role). A follow-up probe via the RDS master
--   credential (AWS Secrets Manager -- the `omninode-postgres-credentials`
--   k8s secret was found STALE in the same pass, a live drift instance of
--   the same class documented in `reference_201_stale_env_postgres_password`)
--   enumerated the exact 8-row gap: `11111111-1111-1111-1111-111111111111`
--   (correlation_id SEED-A-1/2/3) and `22222222-2222-2222-2222-222222222222`
--   (SEED-B-1/2/3), both created_at 2026-07-28 with a timestamp column of
--   2026-07-16 -- pre-existing seed/fixture data, not a real tenant; plus one
--   row each under `d5-e2e-0b5ae67c` (2026-08-16) and
--   `delegation-spotcheck-1786977419` (2026-08-17) -- e2e/spotcheck probe
--   artifacts. None of the four is a real provisioned tenant (cross-checked:
--   none appears in `omninode_cloud.public.tenants`), so none is added to
--   the closed mapping -- doing so would let a probe artifact masquerade as
--   a tenant identity. Disposition: DELETE by exact correlation_id (below,
--   BEFORE the pre-guard), not by tenant_id -- precise removal of the eight
--   known rows, not a sweeping predicate that could catch a future row
--   landing under a reused literal. With this delete applied, the pre-guard
--   below has zero exceptions against the live onex-dev state as of
--   2026-08-18: this migration is expected to apply cleanly, not raise.
--   (The RAISE remains as defense-in-depth for a genuinely new future value.)
--
DELETE FROM delegation_events
WHERE correlation_id IN (
    'SEED-A-1', 'SEED-A-2', 'SEED-A-3',
    'SEED-B-1', 'SEED-B-2', 'SEED-B-3',
    '4ad8a332-4e45-490e-99f4-53e61e8fa05c',
    '549c8c93-1e9a-40dd-917f-9b323fa94b1f'
);
--
-- CONSTRAINTS AND INDEXES SURVIVE THE TYPE CHANGE
--   `idx_delegation_events_tenant_id` (migration 0022) is not dropped or
--   recreated -- PostgreSQL preserves an index across a column type change
--   when the USING expression provides a valid conversion path, which this
--   does.
--
-- RLS POLICY: THE GUC COMPARISON GAINS AN EXPLICIT CAST
--   `current_setting('app.tenant_id', true)` always returns TEXT; the policy
--   predicate must cast it explicitly now that the column is UUID. An
--   unset or malformed GUC value raises on the `::uuid` cast rather than
--   silently coercing -- same accepted posture as 0003 (tracked under
--   OMN-15416 for the real non-owner-pool case, not duplicated here).
--
-- Idempotent: the column-type guard only runs the ALTER when the column is
-- not already UUID, so a second application is a no-op.

DO $$
DECLARE
    v_current_type TEXT;
BEGIN
    SELECT atttypid::regtype::text INTO v_current_type
    FROM pg_attribute
    WHERE attrelid = 'delegation_events'::regclass
      AND attname = 'tenant_id'
      AND NOT attisdropped;

    IF v_current_type IS NULL THEN
        RAISE EXCEPTION
            'OMN-15683: delegation_events.tenant_id column not found -- '
            'expected migration 0022 to have already landed it';
    ELSIF v_current_type = 'uuid' THEN
        RAISE NOTICE
            'delegation_events.tenant_id is already uuid; skipping conversion';
    ELSIF v_current_type = 'text' THEN
        -- Pre-guard: refuse before any DDL runs if any row carries a
        -- tenant_id value outside the closed mapping this migration knows
        -- about. See the OMN-15683 LIVE FINDING note above -- with the
        -- DELETE above applied, this has zero exceptions against the live
        -- onex-dev state as of 2026-08-18; it remains as defense-in-depth
        -- for a genuinely new future value.
        IF EXISTS (
            SELECT 1 FROM delegation_events
            WHERE tenant_id NOT IN (
                'omninode',
                'beta-business-proof',
                'beta-gateway-canary-79afa7263852'
            )
        ) THEN
            RAISE EXCEPTION
                'OMN-15683: no canonical UUID mapping for tenant value % -- '
                'refusing to invent or default one; confirm this is a real, '
                'reviewed tenant identity (cross-check omninode_cloud.public.'
                'tenants), then add it to BOTH _LEGACY_TENANT_UUID_MAP '
                '(omnimarket/projection/tenant_isolation.py) and this '
                'migration''s CASE expression in a follow-up migration',
                (
                    SELECT tenant_id FROM delegation_events
                    WHERE tenant_id NOT IN (
                        'omninode',
                        'beta-business-proof',
                        'beta-gateway-canary-79afa7263852'
                    )
                    LIMIT 1
                );
        END IF;

        -- The pre-existing tenant_isolation POLICY (migration 0023) depends
        -- on this column -- PostgreSQL refuses ALTER COLUMN ... TYPE while
        -- any policy references it, so the policy must be dropped first and
        -- recreated (with the ::uuid cast) after the type change.
        DROP POLICY IF EXISTS tenant_isolation ON delegation_events;

        -- The pre-existing TEXT DEFAULT ('omninode', migration 0022) is not
        -- automatically castable to uuid -- Postgres tries to cast the
        -- DEFAULT expression itself when the column type changes, and
        -- 'omninode'::uuid is not a valid uuid literal, so the DEFAULT must
        -- be dropped before the TYPE change and a new uuid-typed DEFAULT set
        -- after it.
        ALTER TABLE delegation_events
            ALTER COLUMN tenant_id DROP DEFAULT;
        -- Second, independent fail-closed guard: the CASE has no ELSE, so
        -- any value that reaches this point despite the pre-guard above
        -- (e.g. a row inserted between the guard and the ALTER within the
        -- same transaction) evaluates to NULL and is rejected by the
        -- column's existing NOT NULL constraint, aborting the statement.
        ALTER TABLE delegation_events
            ALTER COLUMN tenant_id TYPE UUID
            USING (
                CASE tenant_id
                    WHEN 'omninode' THEN '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid
                    WHEN 'beta-business-proof' THEN '91c74442-1233-4c97-b191-911a10346fdf'::uuid
                    WHEN 'beta-gateway-canary-79afa7263852' THEN '79afa726-3852-464f-b7a4-d4b8b9c75ee7'::uuid
                END
            );
        ALTER TABLE delegation_events
            ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;
    ELSE
        RAISE EXCEPTION
            'OMN-15683: delegation_events.tenant_id has unexpected type %, '
            'expected text or uuid -- operator schema ruling required',
            v_current_type;
    END IF;
END$$;

-- Idempotent whether or not the DO block above dropped it.
DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
CREATE POLICY tenant_isolation ON delegation_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- OMN-14894 ratchet: every file that (re)creates this policy must grant
-- app_dashboard SELECT in the same file. Idempotent; already granted by
-- migration 0023, restated here so this file alone satisfies the ratchet.
GRANT SELECT ON delegation_events TO app_dashboard;
