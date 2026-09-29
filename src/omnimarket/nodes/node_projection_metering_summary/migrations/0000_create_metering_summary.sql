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
CREATE UNIQUE INDEX IF NOT EXISTS metering_summary_key ON public.metering_summary (tenant_id, window_kind, window_start, baseline_model);
