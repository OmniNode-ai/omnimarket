# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for routing policy selection."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSelectionMode(StrEnum):
    EXPLOIT = "exploit"
    EXPLORE = "explore"


class EnumRoutingStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelRankedCandidate(BaseModel):
    """An alternative candidate model ranked by score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., description="Model identifier key.")
    score: float = Field(..., ge=0.0, le=1.0, description="Capability score.")
    cost_per_token: float = Field(..., ge=0.0, description="Cost per token.")
    rank: int = Field(..., ge=1, description="1-indexed rank among alternatives.")


class ModelRoutingPolicyResult(BaseModel):
    """Output of the routing policy engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumRoutingStatus = Field(..., description="ok or error.")
    selected_model_key: str | None = Field(
        default=None, description="Key of the selected model. None when status=error."
    )
    selection_mode: EnumSelectionMode | None = Field(
        default=None,
        description="Whether exploitation or exploration drove the selection. None when status=error.",
    )
    selection_reason: str = Field(
        default="", description="Human-readable reason for the selection."
    )
    alternative_candidates: tuple[ModelRankedCandidate, ...] = Field(
        default=(),
        description="Remaining eligible models ranked by score descending.",
    )
    request_id: str = Field(
        default="", description="Echo of the request correlation ID."
    )
    error: str | None = Field(
        default=None, description="Error detail when status=error."
    )


__all__ = [
    "EnumRoutingStatus",
    "EnumSelectionMode",
    "ModelRankedCandidate",
    "ModelRoutingPolicyResult",
]
