-- OMN-16705: additive re-expression of the positive-bound constraints that were
-- previously added by REWRITING 0001 in place.
--
-- WHY THIS FILE EXISTS
--   0001_create_delegation_routing_tenant_overlay.sql was edited in place TWICE
--   after it had already been applied to a live database:
--
--     0ca6735fa  2026-08-22 18:39  content sha fdf0cc2c...  <- the applied bytes
--                2026-08-22 20:09  applied to the .201 dev lane (omnidash_analytics)
--     88f4ac346  2026-08-22 20:44  content sha 9505c67f...  inline CHECKs added
--     7de798a4a  2026-08-24 11:44  content sha 4cdaf9f2...  CHECKs given names
--
--   platform_catalog.schema_migrations on that lane still records fdf0cc2c,
--   so _ledger/bootstrap.sql's "conflicting migration checksum in canonical node
--   history" predicate raised on every subsequent forward-migration run and the
--   whole one-shot exited 3 (OMN-16705). Applied migration history is permanent
--   (operator ruling 2026-08-04, OMN-15695): 0001 has been restored to its
--   applied bytes and this file carries the delta forward instead.
--
-- WHAT IT DOES
--   1. Nulls out non-positive timeout_ms / max_tokens so the constraints below
--      can be added against pre-existing rows without inventing data.
--   2. Adds the two bound constraints under EXPLICIT names. The names are the
--      point: an unnamed inline CHECK gets a server-generated name on the
--      fresh-create path and no constraint at all on the converged-drift path,
--      so the two paths ended at different schemas -- which is what 7de798a4a
--      was trying to fix by editing 0001.
--   3. Drops the server-generated names an inline CHECK would have produced, so
--      a database created from the superseded 88f4ac346 bytes converges onto the
--      same shape instead of carrying both spellings.
--
--   Every statement is idempotent and safe to re-run: DROP ... IF EXISTS before
--   each ADD, and the UPDATEs match nothing once they have run.
--
-- NOT CARRIED FORWARD, deliberately, and named rather than left silent:
--   88f4ac346 also replaced 0001's duplicate-id reconciliation rank
--   (`row_number() OVER (PARTITION BY id ...)`) with a global rank. That branch
--   is unreachable after 0001 completes -- 0001 ends by adding
--   delegation_routing_tenant_overlay_pkey PRIMARY KEY (id), which cannot
--   succeed while duplicate ids exist -- so a database that has 0001 in its
--   ledger provably has no duplicates for this file to repair. On a fresh
--   database id is BIGSERIAL and unique by construction. The residual is that a
--   drifted table carrying duplicate ids makes 0001 itself fail loudly at its
--   PRIMARY KEY step; that is a fail-closed abort, not silent corruption, and it
--   cannot be corrected without rewriting applied history.

UPDATE delegation_routing_tenant_overlay
SET timeout_ms = NULL
WHERE timeout_ms IS NOT NULL AND timeout_ms <= 0;

UPDATE delegation_routing_tenant_overlay
SET max_tokens = NULL
WHERE max_tokens IS NOT NULL AND max_tokens <= 0;

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_timeout_ms_check;
ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_max_tokens_check;

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_timeout_ms_positive;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT delegation_routing_tenant_overlay_timeout_ms_positive
        CHECK (timeout_ms IS NULL OR timeout_ms > 0);

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_max_tokens_positive;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT delegation_routing_tenant_overlay_max_tokens_positive
        CHECK (max_tokens IS NULL OR max_tokens > 0);
