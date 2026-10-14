# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A route-selecting comparison the base revision did not carry (OMN-20601)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelNewRoutingBranch(BaseModel):
    """A comparison of a routing selector with a string literal, above its count at the base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str = Field(
        ...,
        min_length=1,
        description="<path>::<enclosing scope>::<expression>",
    )
    base_count: int = Field(..., ge=0)
    head_count: int = Field(..., ge=1)


__all__: list[str] = ["ModelNewRoutingBranch"]
