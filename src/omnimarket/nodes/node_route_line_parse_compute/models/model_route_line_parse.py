# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and routing-contract models of the ROUTE line parse."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_classification.model_work_classification import (
    ModelRouteLine,
)


class ModelRouteBandEdge(BaseModel):
    """One band of the routing contract: its rank and the score range it covers."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = Field(min_length=1)
    rank: int = Field(ge=0)
    score_min: int | None
    score_max: int | None


class ModelRoutingContract(BaseModel):
    """The routing contract's data: marker, dimension names and band edges."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    marker: str = Field(min_length=1)
    dimensions: tuple[str, ...] = Field(min_length=1)
    dimension_max: int = Field(ge=1, le=9)
    bands: tuple[ModelRouteBandEdge, ...] = Field(min_length=1)


class EnumRouteParseStatus(StrEnum):
    """PARSED: a valid line. ABSENT: no line. REFUSED: a line that fails a check."""

    PARSED = "parsed"
    ABSENT = "absent"
    REFUSED = "refused"


class ModelRouteLineParseRequest(BaseModel):
    """Text that may carry a ROUTE line, and the routing contract that defines its shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(repr=False)
    routing_contract: ModelRoutingContract


class ModelRouteLineParseResult(BaseModel):
    """The parse outcome. No text of the request is held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumRouteParseStatus
    route: ModelRouteLine | None
    refused_field: str | None
    reason: str | None


__all__ = [
    "EnumRouteParseStatus",
    "ModelRouteBandEdge",
    "ModelRouteLineParseRequest",
    "ModelRouteLineParseResult",
    "ModelRoutingContract",
]
