# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two-sided Wilson 95 percent interval for an error proportion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWilsonInterval(BaseModel):
    """Two-sided Wilson 95 percent interval for an error proportion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    low: float = Field(ge=0.0, le=1.0)
    high: float = Field(ge=0.0, le=1.0)
