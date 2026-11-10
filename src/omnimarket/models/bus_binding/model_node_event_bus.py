# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus a node's client reaches it on, as the node's contract overlay declares it (OMN-22351)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_LANE_ID = r"^[a-z0-9][a-z0-9-]*$"


class ModelNodeEventBus(BaseModel):
    """``event_bus`` of ``<config root>/config/onex/overlays/<node>/overlay.yaml``.

    ``type: kafka`` (the lane's Redpanda) names the lane whose declared broker the client dials;
    the broker address and transport stay in the lane declaration. ``type: inmemory`` hosts the
    node in the caller's process and names no lane. The same shape as the workspace runtime
    configuration's ``event_bus`` block, without its profile fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["kafka", "inmemory"]
    lane: str | None = Field(default=None, pattern=_LANE_ID, max_length=64)

    @model_validator(mode="after")
    def _lane_matches_type(self) -> Self:
        if self.type == "kafka" and self.lane is None:
            raise ValueError("event_bus.type kafka must name the lane it dials")
        if self.type == "inmemory" and self.lane is not None:
            raise ValueError(
                "event_bus.type inmemory has no broker, so it names no lane"
            )
        return self


class ModelNodeEventBusOverlay(BaseModel):
    """A node overlay whose whole document is the node's ``event_bus``.

    Most nodes reached with ``onex run-node`` or a bus client declare nothing else, so this is the
    model the overlay home validates those documents through. An overlay with further keys has its
    own model: the key set is closed here so that a stray key refuses rather than passing unread.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_bus: ModelNodeEventBus


__all__ = ["ModelNodeEventBus", "ModelNodeEventBusOverlay"]
