# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A unit a manifest names, in the native ids an overlay entry may declare it by (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAutomationLivenessUnitReference(BaseModel):
    """One manifest unit; an entry whose native id is any of ``alternatives`` covers it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    alternatives: tuple[str, ...] = Field(min_length=1)
