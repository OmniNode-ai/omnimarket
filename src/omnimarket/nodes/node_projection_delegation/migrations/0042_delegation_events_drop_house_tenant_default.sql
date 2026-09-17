-- OMN-18565: delegation_events.tenant_id stops carrying a house-tenant DEFAULT.
--
-- WHAT THIS CLOSES
-- One correlation's row is written by TWO independent Kafka subscriptions
-- inside the same deployment: the quality-gate verdict
-- (onex.evt.omnibase-infra.quality-gate-result.v1) and the delegation terminal
-- (onex.evt.omnibase-infra.delegation-completed.v1). Both UPSERT on
-- correlation_id, so whichever lands first CREATES the row.
--
-- The verdict carried no tenant. With this DEFAULT in place, a write that said
-- NOTHING about its tenant became a write that ASSERTED one -- the house tenant
-- 820272f9-4aaf-5add-a2df-0af942852ab2 -- authored by the database rather than
-- by any writer. The terminal then upserted the same correlation under the real
-- submitting tenant with app.tenant_id set to it, and because the relation
-- carries FORCE ROW LEVEL SECURITY and the tenant_isolation policy is FOR ALL,
-- PostgreSQL evaluated the policy's USING half against the PRE-EXISTING house
-- row on the ON CONFLICT DO UPDATE path and refused:
--
--     new row violates row-level security policy (USING expression)
--     for table "delegation_events"
--
-- The terminal write never landed, the submitting tenant's reader found nothing
-- in its own partition, and the staging business proof failed on quality_gate.
-- When the terminal won the race instead, the row was created attributed and
-- the proof passed. Measured on the onex-dev staging namespace 2026-09-17:
-- roughly three passes in sixteen proof runs over 24 hours.
--
-- WHY THE DEFAULT IS THE RIGHT THING TO REMOVE, RATHER THAN THE POLICY
-- The policy is doing exactly what it exists to do. What made the collision
-- possible is that an unattributed write was silently given an identity it
-- never claimed, by a mechanism no writer can see and no reader can distinguish
-- from a deliberate attribution. OMN-16831 (operator ruling 2026-08-28, option
-- D) already moved the house-tenant stamp INTO the writer for exactly that
-- reason: the stored byte is the same either way, and what changes is who
-- recorded it. This file finishes that move by removing the fallback the DDL
-- still offered, so a writer that names no tenant is refused instead of being
-- attributed by the schema.
--
-- The house-tenant ruling itself is UNCHANGED. An unattributed TERMINAL is
-- still stamped with the house tenant -- explicitly, by the writer, through
-- house_tenant_write_stamp. What can no longer happen is a DERIVED, partial
-- event authoring a row's tenant as a side effect of having nothing to say.
--
-- NOT NULL IS DELIBERATELY KEPT
-- Dropping the DEFAULT without keeping NOT NULL would trade a wrong tenant for
-- a NULL one, which reads to the tenant_isolation policy as "belongs to
-- nobody", is invisible to every tenant-scoped reader, and is indistinguishable
-- from a row that was never written. NOT NULL is what makes the removal
-- fail-closed: an INSERT that names no tenant now raises 23502 rather than
-- storing a value nobody chose.
--
-- EXISTING ROWS ARE UNTOUCHED
-- A column DEFAULT is consulted only when an INSERT omits the column, so
-- removing it changes no stored row and rewrites no table. Rows already
-- attributed to the house tenant keep that attribution; this migration makes no
-- claim about whether those attributions were correct, and deliberately does
-- not attempt to re-attribute them. Re-keying historical rows would require an
-- authority this file does not have, and the event log those rows project from
-- is the only place that authority exists.
--
-- IDEMPOTENT AND SAFE ON A LANE THAT NEVER CARRIED THE DEFAULT
-- ALTER COLUMN ... DROP DEFAULT is a no-op when no default is present, so this
-- applies cleanly to a lane built from an empty database (where 0007's CREATE
-- TABLE never set one) and to a drifted lane that took 0032/0033/0034's
-- SET DEFAULT arm. It is guarded on the relation's existence only, so a lane
-- that has not yet created delegation_events is skipped rather than aborted.
--
-- SCHEMA-QUALIFICATION: bare, matching every other migration in this chain.
-- delegation_events is classified in the `tenant` LOGICAL domain but lives
-- physically in `public` on every real lane until the OMN-15359 per-family
-- copy; omnibase_infra's TENANT_TABLES_PHYSICALLY_IN_PUBLIC_UNTIL_OMN15359
-- enumerates it for that reason. Writing `tenant.delegation_events` here would
-- address a schema that exists on no lane.
--
-- RLS: untouched. This file neither drops nor recreates the tenant_isolation
-- policy, so the OMN-14894 ratchet (re-issue the app_dashboard grant whenever
-- the policy is recreated) does not apply. The policy compares tenant_id and is
-- indifferent to whether the column has a DEFAULT.

DO $$
BEGIN
    IF to_regclass('delegation_events') IS NULL THEN
        RAISE NOTICE
            'OMN-18565: delegation_events is absent on this lane; nothing to do';
        RETURN;
    END IF;

    ALTER TABLE delegation_events ALTER COLUMN tenant_id DROP DEFAULT;

    -- Stated as an assertion rather than an action: this migration must never
    -- be the thing that relaxes NOT NULL, and a lane that somehow arrived
    -- without it is a lane where dropping the default would start storing NULL
    -- tenants instead of refusing unattributed writes.
    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'delegation_events'
           AND column_name = 'tenant_id'
           AND is_nullable = 'YES'
    ) THEN
        RAISE EXCEPTION
            'OMN-18565: delegation_events.tenant_id is NULLABLE on this lane. '
            'Removing the column DEFAULT here would let an unattributed write '
            'store a NULL tenant, which every tenant-scoped reader treats as '
            'absent. Restore NOT NULL before applying this migration.';
    END IF;
END$$;
