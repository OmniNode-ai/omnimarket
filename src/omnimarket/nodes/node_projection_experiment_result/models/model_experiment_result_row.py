# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scored arm row in the experiment results table."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelExperimentResultRow(BaseModel):
    """One (tenant, experiment, arm, model, measured_at) row.

    Every field of ModelExperimentArmResult under the same name and type, in
    the served column order, plus the three fields the pure fold derives:
    accuracy, advantage_over_baseline and clears_baseline. Declared out
    rather than subclassed so the field order IS the projection_api column
    order, which the contract test asserts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    experiment_id: str = Field(
        min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    arm: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    engine: str = Field(min_length=1, max_length=40)
    n: int = Field(ge=1)
    correct: int = Field(ge=0)
    accuracy: float
    wilson_low: float = Field(ge=0.0, le=1.0)
    wilson_high: float = Field(ge=0.0, le=1.0)
    baseline_accuracy: float = Field(ge=0.0, le=1.0)
    advantage_over_baseline: float
    clears_baseline: bool
    latency_p50_ms: float = Field(ge=0.0)
    latency_p90_ms: float = Field(ge=0.0)
    gpu_seconds: float = Field(ge=0.0)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truth_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_ref: str = Field(default="", max_length=300)
    measured_at: AwareDatetime
