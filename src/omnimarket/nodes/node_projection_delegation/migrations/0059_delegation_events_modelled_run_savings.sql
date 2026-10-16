-- OMN-21015: canonical modelled savings per run and the basis of that figure.
-- Decimal text matches metering_summary's money representation and avoids a
-- float round-trip in either store or snapshot transport. Existing rows remain
-- unpriced; only the metering compute may supply a figure through the writer.
-- No row backfill, baseline guess, or change to the legacy cost_savings_usd.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS modelled_savings_usd TEXT,
    ADD COLUMN IF NOT EXISTS savings_baseline_model TEXT,
    ADD COLUMN IF NOT EXISTS savings_pricing_manifest_version TEXT,
    ADD COLUMN IF NOT EXISTS savings_measurement TEXT;

COMMIT;
