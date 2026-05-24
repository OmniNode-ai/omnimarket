# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for the codebase intelligence bridge effect node."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

QueryStatus = Literal["success", "error", "timeout"]


class ModelCodebaseIntelligenceQueryResponse(BaseModel):
    """Response from HandlerCodebaseIntelligenceBridge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str = Field(description="The operation that was invoked.")
    status: QueryStatus = Field(description="Outcome of the query.")
    result: dict[str, Any] | None = Field(
        default=None,
        description="Raw provider result payload.",
    )
    confidence: str | None = Field(
        default=None,
        description="Confidence level from provider _meta (high/medium/low).",
    )
    retrieval_quality: str | None = Field(
        default=None,
        description="Retrieval quality from provider _meta.",
    )
    stale_warning: str | None = Field(
        default=None,
        description="Stale index warning from provider _meta, if present.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error detail when status is 'error' or 'timeout'.",
    )


__all__ = ["ModelCodebaseIntelligenceQueryResponse", "QueryStatus"]
