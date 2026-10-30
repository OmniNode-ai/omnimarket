# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and decisions for placing a lab work unit on advertising hosts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelHostCapacityAdvertisement,
)

DEFAULT_MAX_LOAD_PER_CORE = 0.75
DEFAULT_MIN_FREE_BYTES = 4 * 1024**3
LAB_WORK_WEIGHT_ENV = "ONEX_LAB_WORK_WEIGHT"


class EnumPlacementDecision(StrEnum):
    PLACED = "placed"
    REFUSED = "refused"


class EnumRefusalReason(StrEnum):
    COULD_NOT_CHECK = "could_not_check"
    OVER_CAPACITY = "over_capacity"
    MISSING_TOOLS = "missing_tools"
    HOST_TABLE = "host_table"


class ModelHostVerdict(BaseModel):
    """Why one host was or was not eligible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    eligible: bool
    reason: str
    load_per_core: float
    mem_available_gb: float
    age_seconds: float


class ModelPlacement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: EnumPlacementDecision
    host_name: str = ""
    refusal: EnumRefusalReason | None = None
    verdicts: list[ModelHostVerdict]
    detail: str = ""

    @property
    def placed(self) -> bool:
        return self.decision is EnumPlacementDecision.PLACED

    def describe(self) -> str:
        lines = [
            f"placement: {self.decision.value}"
            + (f" on {self.host_name}" if self.host_name else "")
            + (f" ({self.refusal.value})" if self.refusal else "")
        ]
        lines.extend(
            f"  {v.host_name}: {'ELIGIBLE' if v.eligible else v.reason} "
            f"load/core={v.load_per_core:.2f} free={v.mem_available_gb:.1f}GB "
            f"age={v.age_seconds:.0f}s"
            for v in self.verdicts
        )
        if self.detail:
            lines.append(self.detail)
        return "\n".join(lines)


class ModelLabPoolPlaceRequest(BaseModel):
    """Advertisements, evaluation time and unit constraints for one placement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    advertisements: tuple[ModelHostCapacityAdvertisement, ...]
    evaluated_at: datetime
    repo: str = ""
    kind: str = "other"
    weight: str | None = None
    max_load_per_core: float = DEFAULT_MAX_LOAD_PER_CORE
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    need_tools: tuple[str, ...] = ()
    only_host: str | None = None
    table: ModelLabHostsTable | None = None


__all__ = [
    "DEFAULT_MAX_LOAD_PER_CORE",
    "DEFAULT_MIN_FREE_BYTES",
    "LAB_WORK_WEIGHT_ENV",
    "EnumPlacementDecision",
    "EnumRefusalReason",
    "ModelHostVerdict",
    "ModelLabPoolPlaceRequest",
    "ModelPlacement",
]
