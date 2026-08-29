-- OMN-16930: the tenant slug -> canonical UUID relation, materialized by the
-- runtime into omnidash_analytics.
--
-- WHY THIS TABLE EXISTS
--   Every slug->UUID conversion on this surface has, until now, inlined a
--   literal map: migration 0031 of node_projection_delegation knows three
--   slugs; omnimarket.projection.tenant_isolation._LEGACY_TENANT_UUID_MAP
--   knows the same three. The live registry (omninode_cloud.public.tenants)
--   held 39 tenants on 2026-08-29 and gains more on every beta signup, so a
--   map authored today is incomplete by construction the moment it is
--   written -- and a migration is immutable once applied.
--
--   0031's own header rejected a mapping relation, correctly, for the
--   information it had: the registry lives in a DIFFERENT DATABASE from
--   omnidash_analytics, unreachable without postgres_fdw/dblink (both
--   AVAILABLE but NOT INSTALLED on the RDS instance; zero foreign servers,
--   verified 2026-08-29). That reasoning treats "reachable" as "reachable
--   synchronously, from inside the RLS predicate". This table is the third
--   option it did not consider: the registry is not JOINED across databases,
--   it is PROJECTED into this one by the runtime, over the bus, exactly the
--   way the other 34 node_projection_* relations are populated. No FDW, no
--   dblink, no cross-database read at apply time -- just a local relation
--   the runtime keeps current.
--
--   Operator ruling, 2026-08-29, verbatim: "Hold + fix mechanism" -- the 0031
--   fence STAYS until write-time stamping deploys, and the permanent fix is
--   green-lit as new scope: a RUNTIME-POPULATED slug->UUID tenant relation in
--   omnidash_analytics so migration-time conversions resolve at apply time
--   instead of hardcoded maps.
--
-- CLASSIFICATION: omninode_internal, NOT tenant. NO RLS. THIS IS LOAD-BEARING.
--   The k8s migrate Job connects as NODE_DB_USER=role_omnidash, which has
--   rolbypassrls=f and is a MEMBER of the owner role but is not the owner.
--   delegation_events carries ENABLE+FORCE RLS, so with app.tenant_id unset
--   that identity sees ZERO rows -- which is precisely why 0031's opening
--   DELETE was a silent no-op, why its IF EXISTS pre-guard never fired, and
--   why the only surviving symptom was the uninformative
--   'column "tenant_id" ... contains null values'. That failure read as a
--   NULL-data problem for a week (OMN-16493 comment 4d7a41a1, findings 1
--   and 3).
--
--   Putting RLS on THIS table would reproduce that blindness in the one place
--   it would be fatal: a conversion that resolves against an
--   invisible-because-RLS-filtered mirror would map every row to NULL and
--   abort with the same uninformative error, or -- worse -- an unguarded
--   variant would silently convert nothing. The mirror MUST be readable by
--   the migrate identity with no GUC set.
--
--   This is not an isolation regression. The mirror holds no tenant's
--   business data: only the (slug, uuid, status) correspondence the platform
--   already publishes at signup and already exposes in the tenant's own JWT.
--   It is a registry index, and a registry index that only one tenant can
--   read is not a registry index.
--
-- POPULATED ONLY BY THE RUNTIME (feedback_only_runtime_touches_database)
--   node_projection_tenant_registry consumes onex.tenant.events -- the
--   durable outbox onex-api already writes in the same transaction that
--   creates the tenant row (OMN-16027) -- and upserts here. Nothing else
--   writes this table. onex-api does not reach into omnidash_analytics; the
--   migration does not reach into omninode_cloud.
--
-- FRESHNESS IS AN INPUT, NOT AN ASSUMPTION
--   observed_at records when this projection last saw the tenant. A
--   conversion that finds a slug missing is not looking at a nonexistent
--   tenant -- it is looking at a projection that has not caught up. The
--   superseding delegation migration says exactly that in its abort message,
--   which is the whole point of the mechanism.

CREATE TABLE IF NOT EXISTS tenant_registry_mirror (
    tenant_slug         TEXT PRIMARY KEY,
    tenant_uuid         UUID NOT NULL,
    display_name        TEXT,
    status              TEXT NOT NULL,
    registry_created_at TIMESTAMPTZ,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_id     TEXT
);

-- ---- BEGIN OMN-15376 shape reconciliation: tenant_registry_mirror ----
-- CREATE TABLE IF NOT EXISTS silently no-ops against a drifted pre-existing
-- table. The guarded adds below converge such a table onto the shape declared
-- above and no-op on the fresh-create path. Every column is added NULLABLE
-- first and tightened afterwards: OMN-16777 proved that
-- "ADD COLUMN IF NOT EXISTS <col> <type> NOT NULL" cannot reconcile a drifted
-- table that holds rows -- PostgreSQL raises 'contains null values' and
-- ON_ERROR_STOP=1 kills the Job, which is the exact deploy-stopping failure
-- the reconciliation exists to prevent. No DROP, no recreate, no TRUNCATE.
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS tenant_slug TEXT;
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS tenant_uuid UUID;
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS registry_created_at TIMESTAMPTZ;
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE tenant_registry_mirror ADD COLUMN IF NOT EXISTS source_event_id TEXT;

-- A row with no slug or no uuid carries no mapping and is worse than absent:
-- it would satisfy a naive EXISTS check while resolving nothing.
--
-- There are exactly two wrong things to do with such a row, and this migration
-- does neither. Backfilling a sentinel invents an identity -- the failure class
-- this whole ticket exists to close. Deleting it destroys data inside an
-- OMN-15376 reconciliation block, which runs against a table whose row count is
-- unknown (the tests/ci/test_node_migration_shape_reconciliation.py rule, and
-- it is the right rule). So this RAISEs and hands the row to an operator.
--
-- In practice this cannot fire on any lane that exists today: this migration
-- CREATEs the relation, no other writer touches it, and the projection's own
-- writer refuses an event that carries no identity. It is here for the drifted
-- pre-existing table the reconciliation idiom exists to converge.
DO $$
DECLARE
    v_unusable BIGINT;
BEGIN
    SELECT count(*) INTO v_unusable
    FROM tenant_registry_mirror
    WHERE tenant_slug IS NULL OR tenant_slug = '' OR tenant_uuid IS NULL;

    IF v_unusable > 0 THEN
        RAISE EXCEPTION
            'OMN-16930: tenant_registry_mirror holds % row(s) with no usable '
            'slug->uuid mapping. Refusing to backfill a sentinel (that would '
            'invent a tenant identity) and refusing to delete them (this is a '
            'shape-reconciliation block and the row count is unknown). Inspect '
            'and dispose of them explicitly, then re-run.', v_unusable;
    END IF;
END$$;

UPDATE tenant_registry_mirror
SET status = COALESCE(NULLIF(status, ''), 'unknown'),
    observed_at = COALESCE(observed_at, NOW());

ALTER TABLE tenant_registry_mirror
    ALTER COLUMN tenant_slug SET NOT NULL,
    ALTER COLUMN tenant_uuid SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN observed_at SET DEFAULT NOW(),
    ALTER COLUMN observed_at SET NOT NULL;

ALTER TABLE tenant_registry_mirror
    DROP CONSTRAINT IF EXISTS tenant_registry_mirror_pkey;
ALTER TABLE tenant_registry_mirror
    ADD CONSTRAINT tenant_registry_mirror_pkey PRIMARY KEY (tenant_slug);
-- ---- END OMN-15376 shape reconciliation: tenant_registry_mirror ----

-- One slug per tenant AND one tenant per slug. The slug uniqueness is the
-- primary key; this is the other direction. Two slugs resolving to the same
-- UUID would silently merge two tenants' rows during a conversion, which is
-- the cross-tenant reassignment OMN-15683 exists to prevent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_registry_mirror_tenant_uuid
    ON tenant_registry_mirror (tenant_uuid);

CREATE INDEX IF NOT EXISTS idx_tenant_registry_mirror_observed_at
    ON tenant_registry_mirror (observed_at DESC);

-- The read grants. Guarded on role existence: the compose lanes run the
-- migration as the `postgres` superuser and do not necessarily have the k8s
-- role set provisioned, so an unguarded GRANT would abort the Job on a lane
-- that is otherwise fine.
--
-- role_omnidash is the migrate Job's own identity -- it must be able to read
-- this table at apply time or the mechanism does not work. app_dashboard is
-- the dashboard reader, granted for parity with every other relation on this
-- surface (the OMN-14894 ratchet's posture).
DO $$
DECLARE
    v_role TEXT;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['role_omnidash', 'app_dashboard']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format(
                'GRANT SELECT ON tenant_registry_mirror TO %I', v_role
            );
        ELSE
            RAISE NOTICE
                'OMN-16930: role % not present on this lane; skipping GRANT '
                '(compose lanes run migrations as the postgres superuser and '
                'do not provision the k8s role set)', v_role;
        END IF;
    END LOOP;
END$$;
