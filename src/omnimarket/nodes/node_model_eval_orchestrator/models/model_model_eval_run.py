# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-model and per-run eval models for node_model_eval_orchestrator (OMN-13615).

Absorbs the ``ModelModelResult`` / ``ModelEvalRun`` shapes from the SEA
``eval/models.py``. These are the orchestrator's *internal* intermediate
records; the node's terminal/emitted contract is the canonical
``ModelExperimentResult`` from omnibase_core (OMN-13613). Strongly typed and
frozen — ``cost_usd`` is a ``Decimal`` to avoid binary-float drift in cost
accounting.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ModelModelEvalResult(BaseModel):
    """One endpoint's evaluation outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(description="Model identifier that produced this result.")
    endpoint: str = Field(description="Endpoint URL the model was called at.")
    contract_passed: bool = Field(
        description="Whether the generated contract+handler passed every gate check.",
    )
    validation_errors: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Validation-gate error messages, empty when the gate passed.",
    )
    schema_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of validation-gate checks passed, in [0.0, 1.0].",
    )
    cost_efficiency_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Cost-efficiency score in [0.0, 1.0] (local endpoints score 1.0).",
    )
    weighted_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="quality_weight*schema + cost_efficiency_weight*cost_efficiency.",
    )
    latency_ms: int = Field(
        default=0, ge=0, description="Call latency in milliseconds."
    )
    cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="Dollar cost of the call (Decimal, never negative).",
    )
    token_usage_input: int = Field(
        default=0, ge=0, description="Prompt tokens consumed."
    )
    token_usage_output: int = Field(
        default=0, ge=0, description="Completion tokens produced."
    )


class ModelModelEvalRun(BaseModel):
    """Aggregate over every endpoint in a single eval run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[ModelModelEvalResult, ...] = Field(
        default_factory=tuple,
        description="Per-endpoint results in request order.",
    )
    best_model: str = Field(
        default="",
        description="model_id with the highest weighted score (empty when none).",
    )
    best_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Highest weighted score across all endpoints.",
    )
    total_cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="Summed dollar cost across all endpoints.",
    )
    total_latency_ms: int = Field(
        default=0, ge=0, description="Summed latency across all endpoints."
    )
    any_endpoint_succeeded: bool = Field(
        default=False,
        description="True when at least one endpoint returned a response.",
    )


__all__ = ["ModelModelEvalResult", "ModelModelEvalRun"]
