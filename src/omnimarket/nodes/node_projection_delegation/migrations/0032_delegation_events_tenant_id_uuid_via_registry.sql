-- OMN-16930: convert delegation_events.tenant_id to UUID by RESOLVING each
-- slug against tenant_registry_mirror at apply time. Supersedes 0031.
--
-- ===========================================================================
-- WHY THIS FILE EXISTS INSTEAD OF AN EDIT TO 0031
-- ===========================================================================
-- 0031's bytes are immutable. Proven, not assumed, against the live .201 dev
-- lane on 2026-08-29 (read-only, as `postgres`, db omnidash_analytics):
--
--   platform_catalog.schema_migrations
--     version  node:node_projection_delegation:0031_delegation_events_tenant_id_to_uuid.sql
--     checksum 79ee3b021d0a04088b2f733fa0558ea110b2a6f75b4fb338abe9c5c123f74442
--     kind     content_sha256
--
-- That value is the sha256 of the committed file (both the omnimarket source
-- and the omnibase_infra vendored twin) and the declared checksum at
-- _ledger/application-migrations.tsv. _ledger/bootstrap.sql compares the two
-- and raises 'conflicting migration checksum in canonical node history' on any
-- divergence, so editing 0031 permanently bricks forward-migration on that
-- lane -- the OMN-16705 incident class verbatim. Supersession is the only
-- legal path. Recorded in _ledger/migration-supersessions.tsv.
--
-- Four-lane sweep behind that verdict: dev has the row and its column is
-- already `uuid`; stability-test's delegation history stops at 0030 and its
-- column is still `text`; prod and judge have no platform_catalog ledger at
-- all. One recorded lane is sufficient to forbid revision.
--
-- ===========================================================================
-- WHAT 0031 GOT WRONG, AND WHAT IS DIFFERENT HERE
-- ===========================================================================
-- 0031 resolved identity from a CLOSED THREE-VALUE LITERAL CASE with no ELSE.
-- The live table holds 169 rows across SIX writing slugs (census 2026-08-29),
-- nine of them outside that map -- including t-1lostguy1, a real, active,
-- externally-owned customer provisioned eight days AFTER 0031 was authored.
-- An unmapped slug evaluated to NULL, tripped 0022's NOT NULL, and surfaced
-- as `column "tenant_id" of relation "delegation_events" contains null
-- values`. The data was never null. The MAP was incomplete, and it was
-- incomplete by construction: the registry gains tenants continuously.
--
-- Three concrete changes:
--
--   1. NO LITERAL SLUG APPEARS IN THE USING CLAUSE. The conversion joins
--      tenant_registry_mirror, which node_projection_tenant_registry keeps
--      current from onex.tenant.events. Whatever the registry knows at apply
--      time is what resolves. Adding a tenant never requires a code change.
--
--   2. THE GUARDS CAN ACTUALLY SEE THE ROWS. 0031's DELETE and its IF EXISTS
--      pre-guard ran as role_omnidash against a table carrying ENABLE+FORCE
--      RLS with `tenant_isolation USING (tenant_id = current_setting(
--      'app.tenant_id', true))`. With that GUC unset the policy matches
--      nothing: the DELETE matched zero rows and the pre-guard's useful RAISE
--      never fired. Only the un-filtered ALTER ... TYPE saw the real rows,
--      which is why every diagnostic 0031 built for exactly this case was
--      blinded (OMN-16493 comment 4d7a41a1, findings 1 and 3). This file
--      defeats that blindness explicitly before it inspects anything.
--
--   3. THE ABORT SAYS WHAT IS ACTUALLY WRONG. A slug missing from the mirror
--      does NOT mean "unknown tenant". It means THE PROJECTION HAS NOT CAUGHT
--      UP. The message says so, names every unresolved slug, and names the
--      node responsible. Fail-closed is preserved -- an unresolvable slug
--      still aborts the whole statement, no row is invented, no row is
--      defaulted, no sentinel survives -- but the operator is now told which
--      lever to pull.
--
-- ===========================================================================
-- DEPLOYMENT ORDERING -- MANDATORY. THIS MIGRATION IS FENCED UNTIL IT HOLDS.
-- ===========================================================================
-- This file is in the OMN-15349 operator fence baseline
-- (omnibase_infra/docker/migrations/forward/fenced-node-migrations.yaml)
-- alongside 0031, and must stay there until ALL of the following are true:
--
--   1. node_projection_tenant_registry is DEPLOYED and CAUGHT UP -- that is,
--      tenant_registry_mirror holds a row for every distinct tenant_id
--      present in delegation_events. Deployed is not enough; caught up is the
--      condition. Verify by running this file's own pre-guard query, not by
--      inspecting pod status.
--   2. Write-time UUID stamping (OMN-16804) is LIVE, so no new slug is ever
--      written to this column after the conversion.
--   3. The OPERATOR un-gates. Steps 1 and 2 are independent and may proceed
--      in parallel; step 3 requires both.
--
-- Step 3 is an operator action. It is explicitly NOT an agent's call, and not
-- something to infer from "the projection looks caught up" -- the fence is the
-- interlock, and a migration that can abort a deploy is not released on
-- judgement. Recorded identically on OMN-16930, OMN-16804 and OMN-16493.
--
-- ===========================================================================
-- IDEMPOTENCE AND LANE SAFETY
-- ===========================================================================
--   * Column already `uuid` (the .201 dev lane, where 0031 applied): NOTICE
--     and skip the conversion. The policy/grant restatement below still runs,
--     so this file alone leaves the relation in the intended end state.
--   * delegation_events absent (a lane that has not run 0007): NOTICE and
--     exit. Nothing to convert.
--   * tenant_registry_mirror absent: the node directories are applied in
--     `sort` order, and node_projection_delegation sorts BEFORE
--     node_projection_tenant_registry -- so on a first-ever bootstrap this
--     file runs before the mirror exists. Handled explicitly below: with an
--     EMPTY delegation_events (exactly the fresh-bootstrap case, since 0007
--     creates it in the same run) the conversion is unambiguous and proceeds;
--     with rows present it aborts and names the ordering violation. Every
--     statement that references the mirror is issued through EXECUTE so this
--     file parses on a lane where the relation does not yet exist.

DO $$
DECLARE
    v_current_type   TEXT;
    v_owner          NAME;
    v_forced         BOOLEAN := FALSE;
    v_assumed_owner  BOOLEAN := FALSE;
    v_mirror         REGCLASS;
    v_row_count      BIGINT;
    v_debris_deleted BIGINT;
    v_unresolved     TEXT;
    v_using_expr     TEXT;
BEGIN
    IF to_regclass('delegation_events') IS NULL THEN
        RAISE NOTICE
            'OMN-16930: delegation_events does not exist on this lane; '
            'nothing to convert';
        RETURN;
    END IF;

    SELECT atttypid::regtype::text INTO v_current_type
    FROM pg_attribute
    WHERE attrelid = 'delegation_events'::regclass
      AND attname = 'tenant_id'
      AND NOT attisdropped;

    IF v_current_type IS NULL THEN
        RAISE EXCEPTION
            'OMN-16930: delegation_events.tenant_id column not found -- '
            'expected migration 0022 to have already landed it';
    ELSIF v_current_type = 'uuid' THEN
        RAISE NOTICE
            'OMN-16930: delegation_events.tenant_id is already uuid; '
            'skipping conversion (0031 applied on this lane)';
        RETURN;
    ELSIF v_current_type <> 'text' THEN
        RAISE EXCEPTION
            'OMN-16930: delegation_events.tenant_id has unexpected type %, '
            'expected text or uuid -- operator schema ruling required',
            v_current_type;
    END IF;

    -- ---------------------------------------------------------------------
    -- Defeat the RLS blindness BEFORE inspecting or disposing of anything.
    --
    -- Everything below is inside this single DO block deliberately: the
    -- runner executes `psql -v ON_ERROR_STOP=1 -f <file>` with NO
    -- --single-transaction, so a multi-statement FORCE toggle that aborted
    -- midway would leave FORCE ROW LEVEL SECURITY permanently OFF on a
    -- tenant-isolated table. A DO block is one transaction; an abort anywhere
    -- below rolls the toggle back with it.
    -- ---------------------------------------------------------------------
    SELECT pg_get_userbyid(relowner), relforcerowsecurity
    INTO v_owner, v_forced
    FROM pg_class
    WHERE oid = 'delegation_events'::regclass;

    IF v_forced THEN
        IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
            RAISE EXCEPTION
                'OMN-16930: delegation_events carries FORCE ROW LEVEL '
                'SECURITY and the migrate identity % is not a member of its '
                'owner role % -- every guard below would be RLS-blinded and '
                'silently see zero rows (the OMN-16493 failure mode). '
                'Refusing to convert half-blind.',
                current_user, v_owner;
        END IF;
        EXECUTE format('SET LOCAL ROLE %I', v_owner);
        v_assumed_owner := TRUE;
        ALTER TABLE delegation_events NO FORCE ROW LEVEL SECURITY;
    END IF;

    -- ---------------------------------------------------------------------
    -- Pre-tenancy debris with no canonical identity, by exact correlation_id.
    --
    -- This is NOT a tenant map and must never be extended into one. These six
    -- rows are the SEED-A/SEED-B fixtures under the literal tenant values
    -- 11111111-... and 22222222-..., neither of which appears in
    -- omninode_cloud.public.tenants -- they have no registry identity to
    -- resolve to, at any point in the future, by construction. Operator
    -- ruling of 2026-08-27, applied on the enumeration recorded in OMN-16493
    -- comment 4d7a41a1: option (a) map-to-canonical for every slug that HAS a
    -- registry UUID -- which is now the JOIN below, not a list -- and option
    -- (c) delete for the rows that have none.
    --
    -- 0031 issued this same DELETE and it matched ZERO rows, because it ran
    -- RLS-blinded. It runs with the policy defeated here, so it does what it
    -- says. Deleting by exact correlation_id rather than by tenant_id is
    -- deliberate: a future row landing under a reused literal must not be
    -- swept up by a predicate written today.
    -- ---------------------------------------------------------------------
    DELETE FROM delegation_events
    WHERE correlation_id IN (
        'SEED-A-1', 'SEED-A-2', 'SEED-A-3',
        'SEED-B-1', 'SEED-B-2', 'SEED-B-3'
    );
    GET DIAGNOSTICS v_debris_deleted = ROW_COUNT;
    RAISE NOTICE
        'OMN-16930: removed % pre-tenancy debris row(s) with no registry '
        'identity', v_debris_deleted;

    SELECT count(*) INTO v_row_count FROM delegation_events;

    v_mirror := to_regclass('tenant_registry_mirror');

    IF v_mirror IS NULL THEN
        IF v_row_count > 0 THEN
            RAISE EXCEPTION
                'OMN-16930: tenant_registry_mirror does not exist on this '
                'lane, but delegation_events holds % row(s) that need their '
                'tenant slug resolved. This migration resolves identity by '
                'JOINing that mirror -- it does not carry a literal map. '
                'ORDERING VIOLATED: node_projection_tenant_registry migration '
                '0000_create_tenant_registry_mirror.sql must be applied, and '
                'the projection must have caught up, BEFORE this file runs. '
                'Node directories are applied in sort order and '
                'node_projection_delegation sorts first, so on a lane with '
                'pre-existing delegation rows the tenant-registry node must '
                'be deployed first. See OMN-16930.',
                v_row_count;
        END IF;
        -- Empty table: nothing to resolve, so the conversion is unambiguous.
        -- This is the first-ever-bootstrap path (0007 creates the table in
        -- the same run). No row evaluates the USING expression.
        RAISE NOTICE
            'OMN-16930: tenant_registry_mirror absent and delegation_events '
            'is empty -- converting with no rows to resolve (fresh bootstrap)';
        v_using_expr := 'NULL::uuid';
    ELSE
        -- -----------------------------------------------------------------
        -- FAIL-CLOSED PRE-GUARD. A slug the mirror cannot resolve aborts.
        --
        -- This is the correct behaviour and it is NOT a defect: it means the
        -- projection has not caught up with the registry. The message says
        -- that explicitly, because the failure it replaces
        -- (`contains null values`) cost a week of misdirected diagnosis by
        -- describing the symptom instead of the cause.
        -- -----------------------------------------------------------------
        EXECUTE $q$
            SELECT string_agg(DISTINCT quote_literal(d.tenant_id), ', '
                              ORDER BY quote_literal(d.tenant_id))
            FROM delegation_events d
            LEFT JOIN tenant_registry_mirror m ON m.tenant_slug = d.tenant_id
            WHERE m.tenant_slug IS NULL
        $q$ INTO v_unresolved;

        IF v_unresolved IS NOT NULL THEN
            RAISE EXCEPTION
                'OMN-16930: tenant_registry_mirror cannot resolve these '
                'delegation_events tenant slugs: %. This does NOT mean the '
                'tenants do not exist -- it means the tenant-registry '
                'projection (node_projection_tenant_registry, consuming '
                'onex.tenant.events) HAS NOT CAUGHT UP with the registry. '
                'Refusing to invent, default, or drop an identity. Resolution: '
                'confirm the projection writer is running and has consumed '
                'the tenant lifecycle events for those slugs, then re-run '
                'this migration. If a slug is genuinely absent from '
                'omninode_cloud.public.tenants it is debris, and its '
                'disposition is an operator ruling recorded on the ticket -- '
                'never a literal added to this file. See OMN-16930.',
                v_unresolved;
        END IF;

        -- -----------------------------------------------------------------
        -- Resolve through a scratch column, NOT a subquery in the USING
        -- clause.
        --
        -- PostgreSQL rejects `ALTER COLUMN ... TYPE ... USING (SELECT ...)`
        -- outright: `ERROR: cannot use subquery in transform expression`
        -- (proven, not guessed -- the first revision of this file was written
        -- with the inline subquery and the scratch-Postgres replay in
        -- tests/test_omn16930_conversion_replay.py failed on exactly that
        -- string). A transform expression may only reference columns of the
        -- row being rewritten.
        --
        -- So the JOIN happens one statement earlier, as a real UPDATE ... FROM
        -- against the mirror, landing the resolved identity in a scratch
        -- column that the transform expression can then reference. This is
        -- still resolution-by-JOIN and still carries no literal slug; the
        -- scratch column exists only inside this transaction and is dropped
        -- below before commit.
        -- -----------------------------------------------------------------
        ALTER TABLE delegation_events
            ADD COLUMN IF NOT EXISTS omn16930_resolved_tenant_uuid UUID;
        EXECUTE $u$
            UPDATE delegation_events d
            SET omn16930_resolved_tenant_uuid = m.tenant_uuid
            FROM tenant_registry_mirror m
            WHERE m.tenant_slug = d.tenant_id
        $u$;
        v_using_expr := 'omn16930_resolved_tenant_uuid';
    END IF;

    -- The pre-existing tenant_isolation POLICY (migration 0023) depends on
    -- this column -- PostgreSQL refuses ALTER COLUMN ... TYPE while any policy
    -- references it, so it is dropped here and recreated with the ::uuid cast
    -- after the block.
    DROP POLICY IF EXISTS tenant_isolation ON delegation_events;

    -- The TEXT DEFAULT ('omninode', migration 0022) is not castable to uuid;
    -- Postgres tries to cast the DEFAULT expression itself during the type
    -- change and 'omninode'::uuid is not a valid uuid literal.
    ALTER TABLE delegation_events ALTER COLUMN tenant_id DROP DEFAULT;

    -- Second, independent fail-closed guard, retained from 0031: the resolving
    -- UPDATE above leaves the scratch column NULL for any value the pre-guard
    -- did not catch (e.g. a row inserted between the guard and the ALTER
    -- inside this transaction), and NULL is rejected by the column's existing
    -- NOT NULL constraint from 0022 -- aborting the statement. No partial
    -- conversion, no invented UUID, no silent passthrough.
    EXECUTE format(
        'ALTER TABLE delegation_events '
        'ALTER COLUMN tenant_id TYPE UUID USING (%s)',
        v_using_expr
    );

    -- The scratch column never outlives this transaction.
    ALTER TABLE delegation_events
        DROP COLUMN IF EXISTS omn16930_resolved_tenant_uuid;

    -- The house tenant UUID: uuid5 of house-tenant.omninode.ai, matching
    -- HOUSE_TENANT_UUID and the DEFAULT 0031 would have set. This is a column
    -- DEFAULT for rows that omit tenant_id, not an identity map -- it resolves
    -- nothing and converts nothing.
    ALTER TABLE delegation_events
        ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;

    IF v_forced THEN
        ALTER TABLE delegation_events FORCE ROW LEVEL SECURITY;
    END IF;
    IF v_assumed_owner THEN
        RESET ROLE;
    END IF;
END$$;

-- Idempotent whether or not the DO block above dropped it, and correct on the
-- lane where 0031 already converted the column.
DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
CREATE POLICY tenant_isolation ON delegation_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- OMN-14894 ratchet: every file that (re)creates this policy must grant
-- app_dashboard SELECT in the same file. Idempotent; already granted by
-- migration 0023, restated here so this file alone satisfies the ratchet.
GRANT SELECT ON delegation_events TO app_dashboard;
