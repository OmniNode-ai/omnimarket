# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class line a capability cell is judged against (plan 13.4, section 2.1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCapabilityLine(BaseModel):
    """Floor, margin, confidence and power of the pass-rate line.

    The defaults are the plan's class threshold of 0.90 with the false-pass
    line's margin, confidence and power, so the required n is what
    ``required_sample_size`` returns for them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor: float = Field(default=0.90, gt=0.0, lt=1.0)
    margin: float = Field(default=0.07, gt=0.0, lt=1.0)
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    power: float = Field(default=0.8, gt=0.0, lt=1.0)
