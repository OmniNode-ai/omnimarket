# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPipelineFillCommand — command input for the pipeline fill orchestrator.

Related:
    - OMN-8688: node_pipeline_fill
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPipelineFillCommand(BaseModel):
    """Command to trigger one pipeline fill cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Cycle correlation ID.")
    top_n: int = Field(
        default=5, ge=1, le=20, description="Maximum tickets to dispatch per cycle."
    )
    wave_cap: int = Field(
        default=5, ge=1, le=20, description="Maximum in-flight dispatches allowed."
    )
    min_score: float = Field(
        default=0.1, ge=0.0, le=1.0, description="Minimum RSD score to dispatch."
    )
    dry_run: bool = Field(
        default=False, description="Score and rank without dispatching."
    )
    state_dir: str = Field(
        default=".onex_state/pipeline-fill",
        description="Directory for dispatched.yaml and other state files.",
    )


__all__: list[str] = ["ModelPipelineFillCommand"]
