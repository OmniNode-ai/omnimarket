# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for dispatch-worker execution effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_dispatch_worker_execution_effect.models.model_dispatch_worker_spec_artifact import (
    ModelDispatchWorkerSpecArtifact,
)


class ModelDispatchWorkerExecutionInput(BaseModel):
    """Specs to execute through the runtime delegation path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Runtime correlation ID.")
    artifacts: tuple[ModelDispatchWorkerSpecArtifact, ...] = Field(
        default=(), description="Already loaded dispatch-worker spec artifacts."
    )
    artifact_paths: tuple[str, ...] = Field(
        default=(), description="Persisted spec artifact paths to load."
    )
    receipt_dir: str = Field(
        default=".onex_state/session/dispatch_execution",
        description="Directory for idempotency receipts.",
    )
    dry_run: bool = Field(default=False, description="Validate only; emit no payloads.")

    @model_validator(mode="after")
    def validate_source(self) -> ModelDispatchWorkerExecutionInput:
        if not self.artifacts and not self.artifact_paths:
            msg = "artifacts or artifact_paths must contain at least one item"
            raise ValueError(msg)
        return self


__all__ = ["ModelDispatchWorkerExecutionInput"]
