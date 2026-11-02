# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the bus binding settle effect."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.bus_binding import ModelBusBindingResolution


class ModelBusBindingSettleRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state_dir: Path
    lane: str = Field(min_length=1)
    observed_at: datetime


class ModelBusBindingSettleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    resolutions: tuple[ModelBusBindingResolution, ...] = ()

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.resolutions if r.resolved)
