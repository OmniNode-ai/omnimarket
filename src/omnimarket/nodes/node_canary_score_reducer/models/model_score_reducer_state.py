# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel, ConfigDict, Field


class ModelCapabilityScoreRow(BaseModel, frozen=True):
    model_key: str
    task_type: str
    avg_recall: float | None = Field(None, ge=0.0, le=1.0)
    avg_precision: float | None = Field(None, ge=0.0, le=1.0)
    avg_fidelity: float | None = Field(None, ge=0.0, le=1.0)
    avg_format_compliance: float | None = Field(None, ge=0.0, le=1.0)
    composite_score: float | None = Field(None, ge=0.0, le=1.0)
    entries_evaluated: int = Field(0, ge=0)
    entries_failed: int = Field(0, ge=0)
    estimated_cost_usd: float | None = Field(None, ge=0.0)
    total_latency_ms: int = Field(0, ge=0)
    canary_run_id: str = ""


class ModelScoreReducerState(BaseModel):
    scores: dict[str, ModelCapabilityScoreRow] = Field(
        default_factory=dict,
        description="Keyed by '{model_key}::{task_type}'",
    )


class ModelMaterializeResult(BaseModel):
    """Typed output of HandlerCanaryScoreReducer.materialize().

    capability_score_rows: upsert targets for the capability_scores table.
    routing_outcome_rows: insert targets for routing_outcomes.quality_score.
    """

    model_config = ConfigDict(frozen=True)

    capability_score_rows: tuple[dict[str, object], ...] = Field(
        default=(),
        description="Rows matching the capability_scores table schema.",
    )
    routing_outcome_rows: tuple[dict[str, object], ...] = Field(
        default=(),
        description="Rows for routing_outcomes.quality_score population.",
    )
