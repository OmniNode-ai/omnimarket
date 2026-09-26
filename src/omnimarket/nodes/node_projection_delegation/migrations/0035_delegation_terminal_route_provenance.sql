-- OMN-17013: versioned terminal-route provenance.
--
-- v1 records remain observable, but are explicitly LEGACY_UNCLASSIFIED: their
-- payload lacks the closed ROUTED|UNROUTED discriminator and cannot be upgraded
-- from endpoint/model/current-config guesses.  v2 will populate the authoritative
-- routing columns after the temporary compat transport contract is released.

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS source_schema_major SMALLINT,
    ADD COLUMN IF NOT EXISTS legacy_classification TEXT,
    ADD COLUMN IF NOT EXISTS routing_disposition TEXT,
    ADD COLUMN IF NOT EXISTS routing_backend_ref TEXT,
    ADD COLUMN IF NOT EXISTS routing_pricing_manifest_version INTEGER;

-- Each guard is name-specific so an interrupted reconciliation reruns safely,
-- while a conflicting pre-existing constraint still fails visibly elsewhere.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_events'::regclass
          AND conname = 'delegation_events_source_schema_major_check'
    ) THEN
        ALTER TABLE delegation_events
            ADD CONSTRAINT delegation_events_source_schema_major_check
            CHECK (source_schema_major IS NULL OR source_schema_major IN (1, 2));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_events'::regclass
          AND conname = 'delegation_events_legacy_classification_check'
    ) THEN
        ALTER TABLE delegation_events
            ADD CONSTRAINT delegation_events_legacy_classification_check
            CHECK (
                legacy_classification IS NULL
                OR legacy_classification = 'LEGACY_UNCLASSIFIED'
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_events'::regclass
          AND conname = 'delegation_events_routing_disposition_check'
    ) THEN
        ALTER TABLE delegation_events
            ADD CONSTRAINT delegation_events_routing_disposition_check
            CHECK (
                routing_disposition IS NULL
                OR routing_disposition IN ('ROUTED', 'UNROUTED')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_events'::regclass
          AND conname = 'delegation_events_routing_provenance_check'
    ) THEN
        ALTER TABLE delegation_events
            ADD CONSTRAINT delegation_events_routing_provenance_check
            CHECK (
                -- Existing rows predate this contract and stay honestly unclassified.
                source_schema_major IS NULL
                OR (
                    source_schema_major = 1
                    AND legacy_classification = 'LEGACY_UNCLASSIFIED'
                    AND routing_disposition IS NULL
                    AND routing_backend_ref IS NULL
                    AND routing_pricing_manifest_version IS NULL
                )
                OR (
                    source_schema_major = 2
                    AND legacy_classification IS NULL
                    AND (
                        (
                            routing_disposition = 'ROUTED'
                            AND routing_backend_ref IS NOT NULL
                            AND btrim(routing_backend_ref) <> ''
                            AND routing_pricing_manifest_version > 0
                        )
                        OR (
                            routing_disposition = 'UNROUTED'
                            AND routing_backend_ref IS NULL
                            AND routing_pricing_manifest_version IS NULL
                        )
                    )
                )
            );
    END IF;
END$$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_delegation_events_routing_disposition
    ON delegation_events (routing_disposition)
    WHERE routing_disposition IS NOT NULL;
