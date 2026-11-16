# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the journal sink's bus mirror (OMN-23877): where each chain event is also published."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelEmissionMirrorOverlay(_Frozen):
    """Every value of the mirror is here, none in code: the bus, the topic of each phase and the time one publication may take.

    A host with no such overlay mirrors nothing and the journal sink behaves as before; the sink says so in its ``mirror_state``.
    """

    bus: Literal["kafka", "inmemory"]
    bus_lane: str | None = None
    # A broker named directly, when this deployment's bus is not on a declared lane; it wins over bus_lane.
    kafka_bootstrap: str | None = None
    topic_start: str = Field(min_length=1)
    topic_success: str = Field(min_length=1)
    topic_failure: str = Field(min_length=1)
    publish_timeout_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _kafka_names_its_broker(self) -> ModelEmissionMirrorOverlay:
        if self.bus == "kafka" and not (self.bus_lane or self.kafka_bootstrap):
            raise ValueError("the kafka bus needs a bus_lane or a kafka_bootstrap")
        return self

    def topic_for(self, phase: Literal["start", "success", "failure"]) -> str:
        return {
            "start": self.topic_start,
            "success": self.topic_success,
            "failure": self.topic_failure,
        }[phase]


__all__ = ["ModelEmissionMirrorOverlay"]
