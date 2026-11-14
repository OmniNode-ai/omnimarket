# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scan of one repository: where each bypass site is and how many there are per category."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_effect_bypass_ratchet_compute.models.enum_bypass import (
    EnumBypassCategory,
)
from omnimarket.nodes.node_effect_bypass_ratchet_compute.models.model_bypass_baseline import (
    ModelBypassCounts,
)


class ModelBypassSite(BaseModel):
    """One line that reaches the outside world outside its effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    category: EnumBypassCategory


class ModelBypassInventory(BaseModel):
    """What the scan found in one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    files_scanned: int = Field(ge=0)
    counts: ModelBypassCounts
    sites: tuple[ModelBypassSite, ...] = ()
