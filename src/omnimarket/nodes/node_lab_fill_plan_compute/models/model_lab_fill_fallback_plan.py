# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only Codex setup failures receive a single Claude fallback."""

from pydantic import BaseModel, ConfigDict

from .model_lab_fill_lane_render import ModelLabFillFallbackItem
from .model_lab_fill_plan import ModelLabFillCapacityResult


class ModelLabFillFallbackPlanRequest(BaseModel):
    """Capacity and current limits are facts read by the effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    placements: tuple[dict[str, object], ...]
    planned: tuple[dict[str, object], ...]
    capacity: ModelLabFillCapacityResult
    limited: tuple[str, ...] = ()
    returned: tuple[dict[str, object] | None, ...] = ()


class ModelLabFillFallbackPlanResult(BaseModel):
    """Ranked choices and returned outcomes keep failed retries visible in status."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    fallbacks: tuple[ModelLabFillFallbackItem, ...]
    skipped: tuple[dict[str, object], ...]
    hosts: dict[str, str]
    outcomes: tuple[dict[str, object], ...]
