-- OMN-17288: supersedes 0032. Same registry-resolved conversion, but the
-- policy recreate and the app_dashboard GRANT happen INSIDE the guarded DO
-- block, so no path can commit with RLS enabled and no policy -- and the
-- documented "nothing to convert" path is actually a no-op.
--
-- ===========================================================================
-- WHY THIS FILE EXISTS INSTEAD OF AN EDIT TO 0032
-- ===========================================================================
-- 0032 is declared in omnibase_infra/docker/migrations/forward/_ledger/
-- application-migrations.tsv, and scripts/validation/check_migration_append_only.py
-- (OMN-16705) refuses any modification to a file declared at the base ref
-- unless the same diff lands a strictly-higher-ordinal successor and records
-- the supersession. This file is that successor.
--
-- Note that this is a stricter rule than "already applied somewhere". 0032 is
-- NOT applied on any lane -- probed 2026-08-31, read-only, as `postgres`,
-- against platform_catalog.schema_migrations on all four .201 lanes:
--
--   dev (omnibase-infra-postgres)              0030, 0031 present; NO 0032
--   stability-test                             0030 present;       NO 0032
--   prod                                       no platform_catalog ledger
--   judge                                      no platform_catalog ledger
--
-- scripts/migrations/check_migration_applied_on_lane.py treats those last two
-- as NO_LEDGER, which it exits non-zero on by design: an indeterminate probe
-- is not a negative answer. So the in-place edit is refused on two independent
-- grounds and supersession is the only path. 0032's bytes are untouched by
-- this change except for one prose comment that carried a live customer's
-- slug (OMN-17288 finding 2), which the supersession row authorises.
--
-- ===========================================================================
-- WHAT 0032 GOT WRONG
-- ===========================================================================
-- Both defects come from the same shape: three statements sat AFTER `END$$`.
--
--   DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
--   CREATE POLICY tenant_isolation ON delegation_events ...;
--   GRANT SELECT ON delegation_events TO app_dashboard;
--
--   (a) THE NO-OP PATH ABORTED THE MIGRATION. 0032's first guard RETURNs when
--       `to_regclass('delegation_events') IS NULL`, announcing "nothing to
--       convert". But RETURN leaves the DO block, not the file: the three
--       statements above still ran, and CREATE POLICY / GRANT on a relation
--       that does not exist raise `relation "delegation_events" does not
--       exist`. The path the file documented as a no-op was the one path that
--       could not complete. Here the guard returns from a block that nothing
--       follows, so it is a no-op in fact and not only in prose.
--
--   (b) RLS ENABLED WITH ZERO POLICIES, BETWEEN TRANSACTIONS. On the
--       conversion path 0032 dropped tenant_isolation inside the DO block and
--       recreated it after `END$$`. The runner is
--       `psql -v ON_ERROR_STOP=1 -f <file>` with NO --single-transaction --
--       0032 says so itself -- so `END$$` COMMITS, and the window between that
--       commit and the standalone CREATE POLICY is a real one. An interruption
--       inside it (operator ^C, pod eviction, connection reset, OOM) leaves a
--       FORCE-RLS table with no policy at all, which denies every application
--       read until someone notices and re-runs. Here the drop and the recreate
--       are the same transaction, so an abort between them rolls the drop back
--       with it.
--
-- A third consequence fell out of (a): 0032's already-`uuid` branch also
-- RETURNed, and relied on those standalone statements to restate the policy.
-- With them gone, that branch must fall THROUGH to the restatement instead --
-- which it does, via v_convert.
--
-- ===========================================================================
-- UNCHANGED FROM 0032, DELIBERATELY
-- ===========================================================================
-- Everything about the identity mechanism is carried over verbatim: no literal
-- slug appears in the resolution path, identity resolves by JOINing
-- tenant_registry_mirror, the fail-closed pre-guard names the projection
-- rather than the data, the RLS blindness is defeated before anything is
-- inspected, and the scratch column never outlives the transaction. The
-- OMN-15361 gate still sees only static statements -- PL/pgSQL prepares a
-- statement's plan on first execution, so the mirror-referencing branch
-- resolves nothing on a lane where the mirror does not yet exist. The
-- OMN-14894 ratchet is satisfied in this file, by the GRANT below.
--
-- ===========================================================================
-- DEPLOYMENT ORDERING -- MANDATORY. THIS MIGRATION IS FENCED UNTIL IT HOLDS.
-- ===========================================================================
-- Identical to 0032's, and this file inherits its place in the OMN-15349
-- operator fence baseline (fenced-node-migrations.yaml) alongside 0031 and
-- 0032. It must stay there until ALL of the following are true:
--
--   1. node_projection_tenant_registry is DEPLOYED and CAUGHT UP -- that is,
--      tenant_registry_mirror holds a row for every distinct tenant_id present
--      in delegation_events. Deployed is not enough; caught up is the
--      condition. Verify with this file's own pre-guard query, not pod status.
--   2. Write-time UUID stamping (OMN-16804) is LIVE, so no new slug is ever
--      written to this column after the conversion.
--   3. The OPERATOR un-gates. Steps 1 and 2 are independent and may proceed in
--      parallel; step 3 requires both.
--
-- Step 3 is an operator action. It is explicitly NOT an agent's call. When it
-- happens, un-gate THIS file and leave 0031 and 0032 fenced: they are retired,
-- and leaving a retired id fenced is what keeps it retired.

DO $$
DECLARE
    v_current_type   TEXT;
    v_owner          NAME;
    v_forced         BOOLEAN := FALSE;
    v_assumed_owner  BOOLEAN := FALSE;
    v_convert        BOOLEAN := TRUE;
    v_mirror         REGCLASS;
    v_row_count      BIGINT;
    v_debris_deleted BIGINT;
    v_unresolved     TEXT;
BEGIN
    -- A true no-op. Nothing follows this block, so RETURN here ends the file.
    IF to_regclass('delegation_events') IS NULL THEN
        RAISE NOTICE
            'OMN-16930: delegation_events does not exist on this lane; '
            'nothing to convert';
        RETURN;
    END IF;

    -- Assignment from a scalar subquery, not `SELECT ... INTO`: the OMN-15361
    -- gate parses a top-level `SELECT ... INTO <name>` as PostgreSQL's SELECT
    -- INTO table-creation form and reports the PL/pgSQL variable as an
    -- unqualified relation target. Same statement, same semantics, and the
    -- catalog reference stays visible to the gate.
    v_current_type := (
        SELECT atttypid::regtype::text
        FROM pg_catalog.pg_attribute
        WHERE attrelid = 'delegation_events'::regclass
          AND attname = 'tenant_id'
          AND NOT attisdropped);

    IF v_current_type IS NULL THEN
        RAISE EXCEPTION
            'OMN-16930: delegation_events.tenant_id column not found -- '
            'expected migration 0022 to have already landed it';
    ELSIF v_current_type = 'uuid' THEN
        -- NOT a RETURN (the 0032 defect). The conversion is skipped, but the
        -- policy and grant restatement below still has to run: on the lane
        -- where 0031 already converted the column, this file is what leaves
        -- the relation in the intended end state.
        RAISE NOTICE
            'OMN-16930: delegation_events.tenant_id is already uuid; '
            'skipping conversion (0031 applied on this lane) and restating '
            'the policy and grant only';
        v_convert := FALSE;
    ELSIF v_current_type <> 'text' THEN
        RAISE EXCEPTION
            'OMN-16930: delegation_events.tenant_id has unexpected type %, '
            'expected text or uuid -- operator schema ruling required',
            v_current_type;
    END IF;

    -- ---------------------------------------------------------------------
    -- Ownership, once, for every path that gets here.
    --
    -- Two things below need it: the FORCE RLS toggle on the conversion path,
    -- and CREATE POLICY / GRANT at the end, which are owner-only on EVERY
    -- path. 0032 acquired it only inside `IF v_forced`, because its policy
    -- statements ran outside the block as whatever identity psql connected as;
    -- moving them inside means the block itself must hold ownership.
    --
    -- Failing here is not a new refusal. A non-owner identity that reached
    -- 0032's standalone CREATE POLICY got `must be owner of table
    -- delegation_events` from Postgres a moment later. This says the same
    -- thing earlier, and names the RLS-blindness consequence that made
    -- OMN-16493 cost a week.
    -- ---------------------------------------------------------------------
    v_owner := (
        SELECT pg_get_userbyid(relowner)
        FROM pg_catalog.pg_class
        WHERE oid = 'delegation_events'::regclass);
    v_forced := (
        SELECT relforcerowsecurity
        FROM pg_catalog.pg_class
        WHERE oid = 'delegation_events'::regclass);

    IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
        RAISE EXCEPTION
            'OMN-16930: the migrate identity % is not a member of '
            'delegation_events'' owner role % -- it can neither restate the '
            'tenant_isolation policy nor, under FORCE ROW LEVEL SECURITY, see '
            'the rows its guards inspect (every one would be RLS-blinded and '
            'silently return zero rows, the OMN-16493 failure mode). Refusing '
            'to convert half-blind.',
            current_user, v_owner;
    END IF;
    -- set_config('role', <name>, is_local => true) is exactly `SET LOCAL ROLE
    -- <name>` and takes the owner as a VALUE, so no SQL text is composed at
    -- runtime and the OMN-15361 gate's dynamic-SQL rejection does not apply.
    -- PL/pgSQL's own `SET` cannot take a variable, which is why an earlier
    -- revision composed one.
    PERFORM set_config('role', v_owner::text, true);
    v_assumed_owner := TRUE;

    IF v_convert THEN
        IF v_forced THEN
            ALTER TABLE delegation_events NO FORCE ROW LEVEL SECURITY;
        END IF;

        -- -----------------------------------------------------------------
        -- Pre-tenancy debris with no canonical identity, by exact
        -- correlation_id.
        --
        -- This is NOT a tenant map and must never be extended into one. These
        -- six rows are the SEED-A/SEED-B fixtures under the literal tenant
        -- values 11111111-... and 22222222-..., neither of which appears in
        -- omninode_cloud.public.tenants -- they have no registry identity to
        -- resolve to, at any point in the future, by construction. Operator
        -- ruling of 2026-08-27, applied on the enumeration recorded in
        -- OMN-16493 comment 4d7a41a1: option (a) map-to-canonical for every
        -- slug that HAS a registry UUID -- which is the JOIN below, not a
        -- list -- and option (c) delete for the rows that have none.
        --
        -- Deleting by exact correlation_id rather than by tenant_id is
        -- deliberate: a future row landing under a reused literal must not be
        -- swept up by a predicate written today.
        -- -----------------------------------------------------------------
        DELETE FROM delegation_events
        WHERE correlation_id IN (
            'SEED-A-1', 'SEED-A-2', 'SEED-A-3',
            'SEED-B-1', 'SEED-B-2', 'SEED-B-3'
        );
        GET DIAGNOSTICS v_debris_deleted = ROW_COUNT;
        RAISE NOTICE
            'OMN-16930: removed % pre-tenancy debris row(s) with no registry '
            'identity', v_debris_deleted;

        v_row_count := (
            SELECT count(*)
            FROM delegation_events);

        v_mirror := to_regclass('tenant_registry_mirror');

        IF v_mirror IS NULL THEN
            IF v_row_count > 0 THEN
                RAISE EXCEPTION
                    'OMN-16930: tenant_registry_mirror does not exist on this '
                    'lane, but delegation_events holds % row(s) that need '
                    'their tenant slug resolved. This migration resolves '
                    'identity by JOINing that mirror -- it does not carry a '
                    'literal map. ORDERING VIOLATED: '
                    'node_projection_tenant_registry migration '
                    '0000_create_tenant_registry_mirror.sql must be applied, '
                    'and the projection must have caught up, BEFORE this file '
                    'runs. Node directories are applied in sort order and '
                    'node_projection_delegation sorts first, so on a lane '
                    'with pre-existing delegation rows the tenant-registry '
                    'node must be deployed first. See OMN-16930.',
                    v_row_count;
            END IF;
            -- Empty table: nothing to resolve, so the conversion is
            -- unambiguous. This is the first-ever-bootstrap path (0007
            -- creates the table in the same run). No row evaluates the USING
            -- expression.
            RAISE NOTICE
                'OMN-16930: tenant_registry_mirror absent and '
                'delegation_events is empty -- converting with no rows to '
                'resolve (fresh bootstrap)';
        ELSE
            -- -------------------------------------------------------------
            -- FAIL-CLOSED PRE-GUARD. A slug the mirror cannot resolve aborts.
            --
            -- This is correct behaviour and NOT a defect: it means the
            -- projection has not caught up with the registry. The message
            -- says so, because the failure it replaces (`contains null
            -- values`) cost a week of misdirected diagnosis by describing the
            -- symptom instead of the cause.
            -- -------------------------------------------------------------
            v_unresolved := (
                SELECT string_agg(DISTINCT quote_literal(d.tenant_id), ', '
                                  ORDER BY quote_literal(d.tenant_id))
                FROM delegation_events d
                LEFT JOIN tenant_registry_mirror m ON m.tenant_slug = d.tenant_id
                WHERE m.tenant_slug IS NULL);

            IF v_unresolved IS NOT NULL THEN
                RAISE EXCEPTION
                    'OMN-16930: tenant_registry_mirror cannot resolve these '
                    'delegation_events tenant slugs: %. This does NOT mean '
                    'the tenants do not exist -- it means the tenant-registry '
                    'projection (node_projection_tenant_registry, consuming '
                    'onex.tenant.events) HAS NOT CAUGHT UP with the registry. '
                    'Refusing to invent, default, or drop an identity. '
                    'Resolution: confirm the projection writer is running and '
                    'has consumed the tenant lifecycle events for those '
                    'slugs, then re-run this migration. If a slug is '
                    'genuinely absent from omninode_cloud.public.tenants it '
                    'is debris, and its disposition is an operator ruling '
                    'recorded on the ticket -- never a literal added to this '
                    'file. See OMN-16930.',
                    v_unresolved;
            END IF;

            -- -------------------------------------------------------------
            -- Resolve through a scratch column, NOT a subquery in the USING
            -- clause. PostgreSQL rejects `ALTER COLUMN ... TYPE ... USING
            -- (SELECT ...)` outright: `ERROR: cannot use subquery in
            -- transform expression` (proven -- the first revision of 0032 was
            -- written that way and the scratch-Postgres replay failed on
            -- exactly that string). A transform expression may only reference
            -- columns of the row being rewritten.
            --
            -- So the JOIN happens one statement earlier, as a real UPDATE ...
            -- FROM against the mirror, landing the resolved identity in a
            -- scratch column the transform expression can reference. Still
            -- resolution-by-JOIN, still no literal slug; the scratch column
            -- exists only inside this transaction and is dropped below.
            -- -------------------------------------------------------------
            ALTER TABLE delegation_events
                ADD COLUMN IF NOT EXISTS omn16930_resolved_tenant_uuid UUID;
            UPDATE delegation_events d
            SET omn16930_resolved_tenant_uuid = m.tenant_uuid
            FROM tenant_registry_mirror m
            WHERE m.tenant_slug = d.tenant_id;
        END IF;

        -- The pre-existing tenant_isolation POLICY (migration 0023) depends on
        -- this column -- PostgreSQL refuses ALTER COLUMN ... TYPE while any
        -- policy references it. Dropped here and recreated at the end of THIS
        -- block, inside the same transaction.
        DROP POLICY IF EXISTS tenant_isolation ON delegation_events;

        -- The TEXT DEFAULT ('omninode', migration 0022) is not castable to
        -- uuid; Postgres tries to cast the DEFAULT expression itself during
        -- the type change and 'omninode'::uuid is not a valid uuid literal.
        ALTER TABLE delegation_events ALTER COLUMN tenant_id DROP DEFAULT;

        -- Second, independent fail-closed guard, retained from 0031: the
        -- resolving UPDATE above leaves the scratch column NULL for any value
        -- the pre-guard did not catch (e.g. a row inserted between the guard
        -- and the ALTER inside this transaction), and NULL is rejected by the
        -- column's existing NOT NULL constraint from 0022 -- aborting the
        -- statement. No partial conversion, no invented UUID, no silent
        -- passthrough.
        -- Two static conversions rather than one statement composed from a
        -- transform-expression string. The branch is the one already decided
        -- above -- mirror absent (and therefore zero rows, proven by the
        -- guard) versus mirror present (identity resolved into the scratch
        -- column) -- so nothing is lost by writing both out, and the
        -- OMN-15361 gate can see and prove each target. The mirror-absent
        -- branch never touches the scratch column, which on that path was
        -- never added.
        IF v_mirror IS NULL THEN
            ALTER TABLE delegation_events
                ALTER COLUMN tenant_id TYPE UUID USING (NULL::uuid);
        ELSE
            ALTER TABLE delegation_events
                ALTER COLUMN tenant_id TYPE UUID
                USING (omn16930_resolved_tenant_uuid);
        END IF;

        -- The scratch column never outlives this transaction.
        ALTER TABLE delegation_events
            DROP COLUMN IF EXISTS omn16930_resolved_tenant_uuid;

        -- The house tenant UUID: uuid5 of house-tenant.omninode.ai, matching
        -- HOUSE_TENANT_UUID and the DEFAULT 0031 would have set. This is a
        -- column DEFAULT for rows that omit tenant_id, not an identity map --
        -- it resolves nothing and converts nothing.
        ALTER TABLE delegation_events
            ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;

        IF v_forced THEN
            ALTER TABLE delegation_events FORCE ROW LEVEL SECURITY;
        END IF;
    END IF;

    -- ---------------------------------------------------------------------
    -- THE FIX. Same transaction as the DROP POLICY above.
    --
    -- On the conversion path this re-establishes the policy the type change
    -- required dropping; on the already-uuid path it restates it so this file
    -- alone leaves the relation in the intended end state. Either way the
    -- relation is never visible to another session with RLS on and no policy,
    -- because it never COMMITS in that state.
    --
    -- The leading DROP is what makes the restatement idempotent: on the
    -- already-uuid path nothing above dropped it, and CREATE POLICY on an
    -- existing name is an error, not a no-op.
    -- ---------------------------------------------------------------------
    DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
    CREATE POLICY tenant_isolation ON delegation_events
      FOR ALL
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

    -- OMN-14894 ratchet: every file that (re)creates this policy must grant
    -- app_dashboard SELECT in the same file. Idempotent; already granted by
    -- migration 0023, restated here so this file alone satisfies the ratchet.
    GRANT SELECT ON delegation_events TO app_dashboard;

    IF v_assumed_owner THEN
        RESET ROLE;
    END IF;
END$$;
