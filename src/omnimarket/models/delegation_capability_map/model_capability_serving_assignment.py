# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a model, backend and configuration is served as today (OMN-18537)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)


class ModelCapabilityServingAssignment(BaseModel):
    """The identity a row must match on all six fields to count for this cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    configuration_id: str = Field(min_length=1)
    identity: ModelCapabilityIdentity

    @model_validator(mode="after")
    def _complete_identity(self) -> Self:
        missing = self.identity.missing_fields()
        if missing:
            raise ValueError(
                f"a serving assignment carries all six identity fields; missing {missing}"
            )
        return self
