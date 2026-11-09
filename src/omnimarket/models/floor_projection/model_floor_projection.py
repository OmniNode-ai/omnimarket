# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A floor input as a bus projection: the envelope a producer publishes and the bundle the floor reads (OMN-23391).

The floor used to read each of these inputs from a file that one producer on one machine kept current. When that
producer stalled the file froze, and every row that read it showed a wrong answer. A projection carries the time the
producer itself stood by the data (``as_of``), the reader keeps that time, and the floor judges it against the
projection's own bound: a projection older than its bound reads unknown, never as the last value it held.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFloorProjectionEnvelope(_Frozen):
    """One publication of one projection: the payload and the time its producer stood by it."""

    projection_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    as_of: datetime
    producer: str = Field(min_length=1)
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def _as_of_names_its_zone(self) -> ModelFloorProjectionEnvelope:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of carries its time zone; a bare time cannot be aged")
        return self


class ModelFloorProjectionSpec(_Frozen):
    """One projection the floor reads: the snapshot topic it is on and how old it may be."""

    topic: str = Field(min_length=1)
    max_age_s: int = Field(gt=0)


class ModelFloorProjectionReadOverlay(_Frozen):
    """Overlay of the reader: the bus it listens on, how long, and the projections the floor takes."""

    bus: Literal["kafka", "inmemory"]
    bus_lane: str | None = None
    # A broker named directly, when this deployment's bus is not on a declared lane; it wins over bus_lane.
    kafka_bootstrap: str | None = None
    read_window_s: float = Field(gt=0)
    projections: dict[str, ModelFloorProjectionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _kafka_names_its_broker(self) -> ModelFloorProjectionReadOverlay:
        if self.bus == "kafka" and not (self.bus_lane or self.kafka_bootstrap):
            raise ValueError("the kafka bus needs a bus_lane or a kafka_bootstrap")
        return self


class ModelFloorProjectionEntry(_Frozen):
    """What one read found for one projection. ``unread`` carries the reason; a payload only comes with ``read``."""

    projection_id: str
    status: Literal["read", "unread"]
    max_age_s: int = Field(gt=0)
    read_at: datetime
    as_of: datetime | None = None
    producer: str | None = None
    reason: str = ""
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> ModelFloorProjectionEntry:
        if self.status == "read" and self.as_of is None:
            raise ValueError("a projection that was read carries its as_of")
        if self.status == "unread" and (self.payload or not self.reason):
            raise ValueError("an unread projection carries its reason and no payload")
        return self


class ModelFloorProjectionBundle(_Frozen):
    """The projections one pass read, written for the floor cycle that follows it."""

    read_at: datetime
    entries: dict[str, ModelFloorProjectionEntry]


class ModelFloorProjectionTickRequest(_Frozen):
    """The flags of one pass of the reader."""

    home: str = Field(min_length=1)


class ModelFloorProjectionTickResult(_Frozen):
    """Where the bundle was written and how many projections were read and unread."""

    output_path: str
    read: int
    unread: int


class ModelFloorProjectionPublishOverlay(_Frozen):
    """Overlay of a producer that publishes one floor projection: the bus it publishes on and the id it publishes under."""

    bus: Literal["kafka", "inmemory"]
    bus_lane: str | None = None
    # A broker named directly, when this deployment's bus is not on a declared lane; it wins over bus_lane.
    kafka_bootstrap: str | None = None
    projection_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def _kafka_names_its_broker(self) -> ModelFloorProjectionPublishOverlay:
        if self.bus == "kafka" and not (self.bus_lane or self.kafka_bootstrap):
            raise ValueError("the kafka bus needs a bus_lane or a kafka_bootstrap")
        return self


class ModelFloorProjectionPublishRequest(_Frozen):
    """One publication pass of a producer: the directory its source lives in."""

    source_dir: str = Field(min_length=1)


class ModelFloorProjectionPublishResult(_Frozen):
    """What one publication pass did."""

    published: bool
    projection_id: str
    as_of: datetime | None = None
    entries: int = 0
    reason: str = ""
