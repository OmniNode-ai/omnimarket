# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A calibration item whose label both judges disagreed with."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceLabelNoiseCase(BaseModel):
    """A calibration item whose label both judges disagreed with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: str = Field(min_length=1)
    label: str = Field(pattern="^(accept|reject)$")
    finding: str = Field(min_length=1)
