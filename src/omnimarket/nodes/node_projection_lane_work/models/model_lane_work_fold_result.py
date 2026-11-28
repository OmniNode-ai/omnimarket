# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B output for the lane work fold (OMN-26708)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneWorkFoldResult(BaseModel):
    """The row one receipt produced, already rendered for the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool = Field(description="True when the receipt was folded.")
    rows: tuple[dict[str, Any], ...] = ()
