# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How many verdict rows were refused, by reason (OMN-20169)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_capability_map.enum_capability_inadmissible_reason import (
    EnumCapabilityInadmissibleReason,
)


class ModelCapabilityInadmissibleCount(BaseModel):
    """One refusal reason and the rows it refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumCapabilityInadmissibleReason
    count: int = Field(ge=1)
