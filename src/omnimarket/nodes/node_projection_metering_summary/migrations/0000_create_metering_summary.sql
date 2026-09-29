-- OMN-19977: replaceable metering snapshots, money preserved as decimal text.
CREATE TABLE IF NOT EXISTS public.metering_summary (
    tenant_id TEXT NOT NULL,
    window_kind TEXT NOT NULL CHECK (window_kind IN ('day', 'all')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    as_of TEXT NOT NULL,
    baseline_model TEXT NOT NULL CHECK (baseline_model <> ''),
    pricing_manifest_version TEXT,
    baseline_state TEXT NOT NULL CHECK (baseline_state IN ('resolved', 'unresolved')),
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

CREATE UNIQUE INDEX IF NOT EXISTS metering_summary_key ON public.metering_summary (tenant_id, window_kind, window_start, baseline_model);
