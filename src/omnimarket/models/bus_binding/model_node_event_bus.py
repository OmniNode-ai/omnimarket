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


__all__ = ["ModelNodeEventBus"]
