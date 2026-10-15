# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A provider's declared quota: how many calls one window allows (OMN-20154).

Declared in ``configs/provider_quota_limits.v1.yaml``. Routing reads the call
count of the same window from the durable ``provider_quota_state`` projection
and withholds a rung whose window is spent, so a call is deferred before the
provider refuses it rather than after the first refusal.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaScope,
)


class ModelProviderQuotaLimit(BaseModel):
    """One provider's declared call limit over one rolling window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(
        min_length=1, description="The quota domain (a provider_quota_policy id)."
    )
    window_seconds: int = Field(ge=1, description="Width of the counted window.")
    limit: int = Field(
        ge=1, description="Calls allowed per window; the call over it is deferred."
    )
    unit: Literal["calls"] = Field(
        description=(
            "What is counted. Only calls are countable: a credit-metered provider "
            "needs token counts an observation does not carry."
        )
    )
    scope: EnumQuotaScope = Field(
        description="Whose counter: one model's row, or the provider-wide row."
    )
    source: str = Field(min_length=1, description="Where the number was read.")
    as_of: date = Field(description="The date the source was read.")


class ModelProviderQuotaLimits(BaseModel):
    """The declared limits document; one entry per provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["provider_quota_limits.v1"]
    limits: tuple[ModelProviderQuotaLimit, ...]

    @model_validator(mode="after")
    def _one_window_per_provider(self) -> ModelProviderQuotaLimits:
        ids = [entry.provider_id for entry in self.limits]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                "a provider declares one window, because the projection keeps one "
                f"window per row; declared twice: {duplicates}"
            )
        return self


__all__ = ["ModelProviderQuotaLimit", "ModelProviderQuotaLimits"]
