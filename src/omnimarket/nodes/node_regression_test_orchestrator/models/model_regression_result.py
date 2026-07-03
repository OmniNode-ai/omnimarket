# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-task regression outcome (absorbed from SEA ``regression/results.py``).

One :class:`ModelRegressionResult` is produced per task in a regression replay.
These per-task outcomes are aggregated by the handler into the single canonical
:class:`~omnibase_core.models.experiment.model_experiment_result.ModelExperimentResult`
the node emits — the node never invents its own top-level result schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRegressionResult(BaseModel):
    """Structured outcome for a single regression task run."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_id: str = Field(..., min_length=1, description="Task this outcome belongs to.")
    passed: bool = Field(..., description="Whether the task produced a passing output.")
    attempt_count: int = Field(0, ge=0, description="Attempts consumed by the task.")
    tier_used: str = Field(
        "", description="Delegation tier (or 'replay' for replayed outcomes)."
    )
    escalation_count: int = Field(
        0, ge=0, description="Tier escalations during the run."
    )
    failure_classes: tuple[str, ...] = Field(
        default_factory=tuple, description="Failure classifications, if any."
    )
    total_tokens: int = Field(0, ge=0, description="Tokens consumed.")
    total_cost_usd: float = Field(0.0, ge=0.0, description="USD cost of the run.")
    latency_ms: int = Field(0, ge=0, description="Wall-clock latency in milliseconds.")
    samples_per_task: int = Field(
        1, ge=1, description="Samples used to derive this outcome."
    )
    provisional: bool = Field(
        False, description="True when derived from a single stochastic run."
    )
    prompt_template_id: str = Field("", description="Prompt template identifier.")
    prompt_template_version: str = Field("", description="Prompt template version.")
    prompt_hash: str = Field("", description="Hash of the rendered prompt.")


__all__ = ["ModelRegressionResult"]
