-- OMN-19977: replaceable metering snapshots, money preserved as decimal text.
CREATE TABLE IF NOT EXISTS public.metering_summary (
    tenant_id TEXT NOT NULL,
    window_kind TEXT NOT NULL CONSTRAINT ck_metering_summary_window_kind CHECK (window_kind IN ('day', 'all')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    as_of TEXT NOT NULL,
    baseline_model TEXT NOT NULL CONSTRAINT ck_metering_summary_baseline_model CHECK (baseline_model <> ''),
    pricing_manifest_version TEXT,
    baseline_state TEXT NOT NULL CONSTRAINT ck_metering_summary_baseline_state CHECK (baseline_state IN ('resolved', 'unresolved')),
    runs_total INTEGER NOT NULL,
    runs_measured INTEGER NOT NULL,
    runs_unknown_tokens INTEGER NOT NULL,
    runs_unknown_spend INTEGER NOT NULL,
    tokens_in BIGINT NOT NULL,
    tokens_out BIGINT NOT NULL,
    spend_usd TEXT,
    counterfactual_usd TEXT,
    savings_usd TEXT,
    summary_json TEXT NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: metering_summary ----
-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so
-- CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS tenant_id                TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS window_kind              TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS window_start             TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS window_end               TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS as_of                    TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS baseline_model           TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS pricing_manifest_version TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS baseline_state           TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS runs_total               INTEGER;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS runs_measured            INTEGER;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS runs_unknown_tokens      INTEGER;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS runs_unknown_spend       INTEGER;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS tokens_in                BIGINT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS tokens_out               BIGINT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS spend_usd                TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS counterfactual_usd       TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS savings_usd              TEXT;
ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS summary_json             TEXT;

-- Refuse to invent values for pre-existing rows that violate NOT NULL: SET NOT NULL
-- fails the migration when a row holds NULL (a data ruling is needed: backfill,
-- or drop the NOT NULL from the contract).
ALTER TABLE public.metering_summary ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN window_kind SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN window_start SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN window_end SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN as_of SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN baseline_model SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN baseline_state SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN runs_total SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN runs_measured SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN runs_unknown_tokens SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN runs_unknown_spend SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN tokens_in SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN tokens_out SET NOT NULL;
ALTER TABLE public.metering_summary ALTER COLUMN summary_json SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.metering_summary'::regclass AND conname = 'ck_metering_summary_window_kind'
    ) THEN
        ALTER TABLE public.metering_summary ADD CONSTRAINT ck_metering_summary_window_kind CHECK (window_kind IN ('day', 'all'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.metering_summary'::regclass AND conname = 'ck_metering_summary_baseline_model'
    ) THEN
        ALTER TABLE public.metering_summary ADD CONSTRAINT ck_metering_summary_baseline_model CHECK (baseline_model <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.metering_summary'::regclass AND conname = 'ck_metering_summary_baseline_state'
    ) THEN
        ALTER TABLE public.metering_summary ADD CONSTRAINT ck_metering_summary_baseline_state CHECK (baseline_state IN ('resolved', 'unresolved'));
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: metering_summary ----

CREATE UNIQUE INDEX IF NOT EXISTS metering_summary_key ON public.metering_summary (tenant_id, window_kind, window_start, baseline_model);
