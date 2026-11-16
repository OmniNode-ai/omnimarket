-- OMN-24171. Owned by omnimarket.nodes.node_projection_eval_results.
-- Four tenant-scoped stores: every scored try (eval_item_scores), every trace part
-- (eval_item_traces), a run's close (eval_run_closes) and each run's rate (eval_run_rates).
-- The items and traces beside context_roi_scores are the full item and trace store; the code
-- trial's one-row result keeps its fingerprint there.
CREATE TABLE IF NOT EXISTS public.eval_item_scores (
    tenant_id UUID NOT NULL,
    item_key TEXT NOT NULL,
    event_id UUID NOT NULL,
    run_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL DEFAULT '',
    replicate INT NOT NULL DEFAULT 0,
    try_ordinal INT NOT NULL DEFAULT 1,
    task_set_id TEXT NOT NULL,
    task_set_digest TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    size_band TEXT NOT NULL,
    engine TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL DEFAULT '',
    generation_settings_hash TEXT NOT NULL DEFAULT '',
    prompt_template_version TEXT NOT NULL DEFAULT '',
    context_arm TEXT NOT NULL,
    context_pack_hash TEXT NOT NULL DEFAULT '',
    pack_profile_hash TEXT NOT NULL DEFAULT '',
    prompt_tokens INT NOT NULL DEFAULT 0,
    scorer TEXT NOT NULL,
    scorer_version TEXT NOT NULL DEFAULT '',
    field_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
    passed BOOLEAN,
    unscorable_reason TEXT,
    step_verdict TEXT NOT NULL DEFAULT 'none',
    step_verdict_source TEXT NOT NULL DEFAULT 'deterministic',
    scored_at TIMESTAMPTZ NOT NULL,
    trace_sha256 TEXT NOT NULL DEFAULT '',
    scoring_id UUID NOT NULL,
    lane TEXT NOT NULL DEFAULT 'dev',
    host TEXT NOT NULL DEFAULT '',
    install_revision TEXT NOT NULL DEFAULT '',
    outcome_digest TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, item_key)
);
CREATE INDEX IF NOT EXISTS eval_item_scores_run_idx
    ON public.eval_item_scores (tenant_id, run_id, model_id);

CREATE TABLE IF NOT EXISTS public.eval_item_traces (
    tenant_id UUID NOT NULL,
    item_key TEXT NOT NULL,
    scoring_id UUID NOT NULL,
    part_index INT NOT NULL,
    part_count INT NOT NULL,
    trace_sha256 TEXT NOT NULL,
    chunk_b64 TEXT NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, item_key, scoring_id, part_index)
);
CREATE INDEX IF NOT EXISTS eval_item_traces_scoring_idx
    ON public.eval_item_traces (tenant_id, scoring_id);

CREATE TABLE IF NOT EXISTS public.eval_run_closes (
    tenant_id UUID NOT NULL,
    run_id TEXT NOT NULL,
    series TEXT NOT NULL DEFAULT '',
    task_sample_digest TEXT NOT NULL DEFAULT '',
    closed_at TIMESTAMPTZ NOT NULL,
    abandoned BOOLEAN NOT NULL DEFAULT FALSE,
    void_reason TEXT NOT NULL DEFAULT '',
    item_count INT NOT NULL,
    sequences JSONB NOT NULL DEFAULT '[]'::jsonb,
    band_usable JSONB NOT NULL DEFAULT '{}'::jsonb,
    band_target JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome_digest TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE IF NOT EXISTS public.eval_run_rates (
    tenant_id UUID NOT NULL,
    run_id TEXT NOT NULL,
    scorer TEXT NOT NULL,
    task_set_digest TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL DEFAULT '',
    generation_settings_hash TEXT NOT NULL DEFAULT '',
    prompt_template_version TEXT NOT NULL DEFAULT '',
    scorer_version TEXT NOT NULL DEFAULT '',
    context_arm TEXT NOT NULL DEFAULT '',
    replicate INT NOT NULL DEFAULT 0,
    size_band TEXT NOT NULL,
    n INT NOT NULL,
    passed INT NOT NULL,
    unscorable_model INT NOT NULL,
    unscorable_infrastructure INT NOT NULL,
    wilson_low DOUBLE PRECISION NOT NULL,
    wilson_high DOUBLE PRECISION NOT NULL,
    first_scored_at TIMESTAMPTZ NOT NULL,
    last_scored_at TIMESTAMPTZ NOT NULL,
    series TEXT NOT NULL DEFAULT '',
    run_state TEXT NOT NULL,
    void_reason TEXT NOT NULL DEFAULT '',
    task_sample_digest TEXT NOT NULL DEFAULT '',
    band_usable JSONB NOT NULL DEFAULT '{}'::jsonb,
    band_target JSONB NOT NULL DEFAULT '{}'::jsonb,
    item_count INT NOT NULL DEFAULT 0,
    scorer_versions JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (
        tenant_id, run_id, scorer, task_set_digest, engine, model_id, model_revision,
        generation_settings_hash, prompt_template_version, scorer_version, context_arm,
        replicate, size_band
    )
);

ALTER TABLE public.eval_item_scores ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_item_scores;
CREATE POLICY tenant_isolation ON public.eval_item_scores
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_item_scores TO app_dashboard;

ALTER TABLE public.eval_item_traces ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_item_traces;
CREATE POLICY tenant_isolation ON public.eval_item_traces
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_item_traces TO app_dashboard;

ALTER TABLE public.eval_run_closes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_run_closes;
CREATE POLICY tenant_isolation ON public.eval_run_closes
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_run_closes TO app_dashboard;

ALTER TABLE public.eval_run_rates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_run_rates;
CREATE POLICY tenant_isolation ON public.eval_run_rates
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_run_rates TO app_dashboard;
