# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ratchet's request and verdict: the scan and the baseline in, the per-category reading out."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_effect_bypass_ratchet_compute.models.enum_bypass import (
    EnumBypassCategory,
    EnumBypassFinding,
)
from omnimarket.nodes.node_effect_bypass_ratchet_compute.models.model_bypass_baseline import (
    ModelBypassBaseline,
)
from omnimarket.nodes.node_effect_bypass_ratchet_compute.models.model_bypass_inventory import (
    ModelBypassInventory,
)


class ModelBypassRatchetRequest(BaseModel):
    """One repository's scan, its baseline, and optionally the baseline the merge base carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inventory: ModelBypassInventory
    baseline: ModelBypassBaseline
    base_baseline: ModelBypassBaseline | None = None


class ModelBypassCategoryReading(BaseModel):
    """One category: the count now, the count the baseline allows, and whether it rose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: EnumBypassCategory
    now: int = Field(ge=0)
    baseline: int = Field(ge=0)
    rose: bool


class ModelBypassRatchetVerdict(BaseModel):
    """The ratchet's reading. ok is false when any category rose or the baseline itself grew."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    ok: bool
    total_now: int = Field(ge=0)
    total_baseline: int = Field(ge=0)
    categories: tuple[ModelBypassCategoryReading, ...]
    findings: tuple[EnumBypassFinding, ...] = ()
