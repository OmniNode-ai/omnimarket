# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for the codebase intelligence bridge effect node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OperationType = Literal[
    "get_answer",
    "get_context",
    "get_symbol",
    "search_codebase",
    "get_why",
]


class ModelCodebaseIntelligenceQueryRequest(BaseModel):
    """Input for HandlerCodebaseIntelligenceBridge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: OperationType = Field(
        description="Which codebase intelligence operation to invoke.",
    )
    query: str = Field(
        description="The query string or symbol path to pass to the provider.",
    )
    targets: tuple[str, ...] = Field(
        default=(),
        description="Optional target files, modules, or symbols to scope the query.",
    )
    include: tuple[str, ...] = Field(
        default=(),
        description="Optional include flags for get_context (e.g. 'callers', 'ownership').",
    )


__all__ = ["ModelCodebaseIntelligenceQueryRequest", "OperationType"]
