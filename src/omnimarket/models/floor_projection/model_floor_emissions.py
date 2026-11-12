# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the floor emissions projection: the overlay of its publisher and the result of one pass (OMN-23877)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFloorEmissionsOverlay(_Frozen):
    """Overlay of the node that folds the floor's emission topics into one projection: every value is here, none in code."""

    bus: Literal["kafka", "inmemory"]
    bus_lane: str | None = None
    # A broker named directly, when this deployment's bus is not on a declared lane; it wins over bus_lane.
    kafka_bootstrap: str | None = None
    read_window_s: float = Field(gt=0)
    projection_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    snapshot_topic: str = Field(min_length=1)
    emission_topics: tuple[str, ...] = Field(min_length=1)
    window_hours: int = Field(gt=0)
    max_events: int = Field(gt=0)

    @model_validator(mode="after")
    def _kafka_names_its_broker(self) -> ModelFloorEmissionsOverlay:
        if self.bus == "kafka" and not (self.bus_lane or self.kafka_bootstrap):
            raise ValueError("the kafka bus needs a bus_lane or a kafka_bootstrap")
        return self


class ModelFloorEmissionsPublishResult(_Frozen):
    """Whether the projection was published, when it was read and how many events and publications it held."""

    published: bool
    projection_id: str
    as_of: datetime | None = None
    events: int = Field(ge=0, default=0)
    skipped: int = Field(ge=0, default=0)
    reason: str = ""
