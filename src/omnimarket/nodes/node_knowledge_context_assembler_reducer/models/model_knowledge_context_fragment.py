# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A single backend response fragment collected by the reducer."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnumFragmentSource",
    "ModelKnowledgeContextFragment",
]


class EnumFragmentSource(StrEnum):
    CODEBASE_INTELLIGENCE = "codebase_intelligence"
    ANTIPATTERN_MATCH = "antipattern_match"
    AGENT_LEARNING_RETRIEVAL = "agent_learning_retrieval"
    ARCHITECTURE_GRAPH = "architecture_graph"


class ModelKnowledgeContextFragment(BaseModel):
    """One backend's contribution to the assembled context bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fragment_source: EnumFragmentSource = Field(
        ..., description="Which backend produced this fragment"
    )
    content: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw payload from the backend (empty when error is set)",
    )
    correlation_id: str = Field(
        ..., description="Correlation ID from the original request"
    )
    error: str | None = Field(
        default=None,
        description="Error message when the backend failed; None on success",
    )

    @property
    def ok(self) -> bool:
        return self.error is None
