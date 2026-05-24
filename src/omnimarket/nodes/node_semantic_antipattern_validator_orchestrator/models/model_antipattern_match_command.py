# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command emitted by the orchestrator for node_antipattern_match_effect to consume."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternMatchCommand(BaseModel):
    """Command dispatched to the antipattern match effect (Qdrant lookup).

    Carries all parameters the effect needs to perform the similarity search
    and return candidate matches for the classifier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    file_path: str
    file_content: str
    enforcement_mode: str
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    correlation_id: str


__all__ = ["ModelAntipatternMatchCommand"]
