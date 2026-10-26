# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a request's routing chain starts, from the capability map (OMN-20170)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)


class ModelCapabilityStart(BaseModel):
    """Where the chain starts for one request, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    size_band: EnumSizeBand | None
    start_tier_name: str | None
    reason: str = Field(min_length=1)
    cell: ModelCapabilityCell | None = None

    def describe(self) -> str:
        """The routing log line: class, band, chosen cell, its n and its bound."""
        band = self.size_band.value if self.size_band is not None else "none"
        if self.cell is None:
            return (
                f"capability start: class={self.task_class} band={band} "
                f"tier={self.start_tier_name or 'static'} cell=none "
                f"reason={self.reason}"
            )
        cell = self.cell
        return (
            f"capability start: class={self.task_class} band={band} "
            f"tier={self.start_tier_name} cell={cell.model_key}/{cell.backend_id}/"
            f"{cell.configuration_id} n={cell.n} bound={cell.decision_lower_bound:.4f} "
            f"map_run={cell.map_run_id} reason={self.reason}"
        )
