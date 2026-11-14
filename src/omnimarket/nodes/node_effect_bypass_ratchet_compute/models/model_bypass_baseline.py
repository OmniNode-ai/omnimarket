# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shrink-only baseline: today's bypass count per category, per repository."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_effect_bypass_ratchet_compute.models.enum_bypass import (
    EnumBypassCategory,
)


class ModelBypassCounts(BaseModel):
    """Bypass sites per category. A category that is absent counts zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    counts: dict[EnumBypassCategory, int] = Field(default_factory=dict)

    def get(self, category: EnumBypassCategory) -> int:
        return self.counts.get(category, 0)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class ModelBypassBaseline(BaseModel):
    """The baseline file: one counts block per repository, and the ticket that owns its burn-down."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(ge=1)
    ticket: str = Field(min_length=1)
    repositories: dict[str, ModelBypassCounts]
