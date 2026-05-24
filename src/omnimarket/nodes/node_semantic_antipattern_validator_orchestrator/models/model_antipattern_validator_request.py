# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the semantic antipattern validator orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternValidatorRequest(BaseModel):
    """Input to the orchestrator: file to validate + policy parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str
    file_content: str
    enforcement_mode: str = Field(default="blocking")
    similarity_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    correlation_id: str


__all__ = ["ModelAntipatternValidatorRequest"]
