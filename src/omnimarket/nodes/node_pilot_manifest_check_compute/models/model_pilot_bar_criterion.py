# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One criterion of a pilot's frozen bar."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_bar_comparator import (
    EnumPilotBarComparator,
)


class ModelPilotBarCriterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    comparator: EnumPilotBarComparator
    threshold: float
