# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the host tick reads to run one host's declared-clone refresh (OMN-23557).

The tick is the unit the host installer renders: it refreshes this host's own declared clone and
writes the unit's evidence. Every deployment value comes from the overlay, the host table, the
contract or the environment; nothing is hard-coded in the tick.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDeclaredCloneTickOverlay(BaseModel):
    """How one host's tick opens its bus and waits for its receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory", "redpanda"]
    bus_lane: str | None = None
    host_name_env: str = Field(min_length=1)
    wait_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_lane(self) -> Self:
        if self.backend == "redpanda" and not (self.bus_lane and self.bus_lane.strip()):
            raise ValueError("the redpanda backend requires a non-empty bus_lane")
        if self.backend == "inmemory" and self.bus_lane is not None:
            raise ValueError("the inmemory backend forbids a bus_lane")
        return self


__all__ = ["ModelDeclaredCloneTickOverlay"]
