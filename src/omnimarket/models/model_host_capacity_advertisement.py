# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pool host's capacity advertisement (OMN-20867).

The lab-work node publishes it on each pool host; the lab-fill planner reads it as
that host's headroom. Neither imports the other's models, so it lives here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelHostCapacityAdvertisement(BaseModel):
    """One pool host's capacity at ``advertised_at``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    cores: int = Field(..., ge=1)
    load1: float = Field(..., ge=0.0)
    mem_available_bytes: int = Field(..., ge=0)
    tools: list[str] = Field(default_factory=list)
    running_units: int = Field(default=0, ge=0)
    max_units: int = Field(default=1, ge=1)
    #: Added to load per core for RANKING only, never for the load bar: it keeps an
    #: evidence-lane host last-resort while idle hosts take the work (OMN-17485).
    rank_penalty: float = Field(default=0.0, ge=0.0)
    advertised_at: datetime
    cadence_seconds: int = Field(..., ge=1)

    @property
    def load_per_core(self) -> float:
        """Load per core, counting the units this host already runs."""
        return (self.load1 + self.running_units) / self.cores
