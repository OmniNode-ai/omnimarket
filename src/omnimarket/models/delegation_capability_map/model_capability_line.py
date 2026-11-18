# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class line a capability cell is judged against (plan 13.4, section 2.1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCapabilityLine(BaseModel):
    """Floor, margin, confidence and power of the pass-rate line.

    The values come from the overlay key ``delegation.capability_map.line`` of the
    node ``node_projection_delegation_capability_map``; there is no packaged
    value, so every field is required.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor: float = Field(gt=0.0, lt=1.0)
    margin: float = Field(gt=0.0, lt=1.0)
    confidence: float = Field(gt=0.5, lt=1.0)
    power: float = Field(gt=0.0, lt=1.0)
