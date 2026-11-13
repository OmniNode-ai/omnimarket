-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- OMN-24286: one row per scored experiment arm and measurement time.
--
-- The projection node_projection_experiment_result writes this table from the recorded
-- experiment-arm-result event, and serves it on the snapshot topic
-- onex.snapshot.projection.eval.experiment-results.v1 for the evals dashboard. A new
-- measured_at for the same arm is a new row, so accuracy over time keeps its history; a
-- redelivered event hits the primary key and changes nothing.

CREATE TABLE IF NOT EXISTS public.experiment_arm_results (
    tenant_id UUID NOT NULL,
    experiment_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    model_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    n INT NOT NULL CHECK (n >= 1),
    correct INT NOT NULL CHECK (correct >= 0 AND correct <= n),
    accuracy DOUBLE PRECISION NOT NULL,
    wilson_low DOUBLE PRECISION NOT NULL,
    wilson_high DOUBLE PRECISION NOT NULL,
    baseline_accuracy DOUBLE PRECISION NOT NULL,
    advantage_over_baseline DOUBLE PRECISION NOT NULL,
    clears_baseline BOOLEAN NOT NULL,
    latency_p50_ms DOUBLE PRECISION NOT NULL,
    latency_p90_ms DOUBLE PRECISION NOT NULL,
    gpu_seconds DOUBLE PRECISION NOT NULL,
    corpus_sha256 TEXT NOT NULL,
    truth_sha256 TEXT NOT NULL,
    report_ref TEXT NOT NULL DEFAULT '',
    measured_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, experiment_id, arm, model_id, measured_at)
);

CREATE INDEX IF NOT EXISTS experiment_arm_results_tenant_experiment_measured_idx
    ON public.experiment_arm_results (tenant_id, experiment_id, measured_at DESC);
