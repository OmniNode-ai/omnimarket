# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelCanaryReport -- output contract for node_adr_canary_orchestrator.

[OMN-10698]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelModelScore(BaseModel):
    """Aggregated score for a single model across all evaluated manifest entries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(
        ..., description="Model key (e.g. 'qwen3-coder', 'deepseek-r1')."
    )
    entries_evaluated: int = Field(default=0, ge=0)
    entries_failed: int = Field(default=0, ge=0)

    avg_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    avg_precision: float | None = Field(default=None, ge=0.0, le=1.0)
    avg_fidelity: float | None = Field(default=None, ge=0.0, le=1.0)
    avg_format_compliance: float | None = Field(default=None, ge=0.0, le=1.0)

    total_latency_ms: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)


class ModelCanaryReport(BaseModel):
    """Result produced by the ADR canary orchestrator after a full pipeline run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(
        ..., description="Unique run identifier (YYYYMMDD-HHMMSS-<random6>)."
    )
    manifest_path: str = Field(..., description="Manifest path that was evaluated.")
    entries_total: int = Field(
        default=0, ge=0, description="Total manifest entries scheduled."
    )
    entries_completed: int = Field(
        default=0, ge=0, description="Entries that completed pipeline."
    )
    entries_failed: int = Field(
        default=0, ge=0, description="Entries that failed at any stage."
    )

    model_scores: list[ModelModelScore] = Field(
        default_factory=list,
        description="Per-model aggregated scores across all entries.",
    )

    evidence_dir: str = Field(
        ...,
        description="Absolute path to the evidence bundle directory for this run.",
    )
    scorecard_path: str = Field(
        ...,
        description="Absolute path to the generated scorecard.md file.",
    )

    dry_run: bool = Field(default=False)
    success: bool = Field(default=True)
    error_message: str | None = Field(default=None)


__all__: list[str] = ["ModelCanaryReport", "ModelModelScore"]
