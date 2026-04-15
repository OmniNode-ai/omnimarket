# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for node_session_compose."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .model_phase_result import ModelPhaseResult

__all__ = ["ModelSessionComposeResult"]


class ModelSessionComposeResult(BaseModel):
    """Result of a session compose orchestration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(
        ...,
        description="Overall success of the compose orchestration",
    )
    dry_run: bool = Field(
        ...,
        description="Whether the compose was a dry-run",
    )
    phase_results: list[ModelPhaseResult] = Field(
        default_factory=list,
        description="Per-phase execution results",
    )
