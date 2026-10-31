# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelEvidencePipelineCommand — trigger payload for evidence validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.evidence_pipeline.wire.types import TriggerSurface


class ModelEvidencePipelineCommand(BaseModel):
    """Command payload that starts the per-PR evidence pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    validation_run_id: str = Field(..., min_length=1)
    ticket_id: str = Field(..., min_length=1)
    repository: str = Field(..., min_length=1)
    source_commit_sha: str = Field(..., min_length=7)
    requested_at: str = Field(..., min_length=1)
    trigger_surface: TriggerSurface
    source_pr: int | None = Field(default=None, ge=1)
    deployment_id: str | None = Field(default=None, min_length=1)
    topology_affecting: bool = False
    metadata: Mapping[str, str] = Field(default_factory=dict)


__all__: list[str] = ["ModelEvidencePipelineCommand"]
