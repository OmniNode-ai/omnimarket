# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier's answer: an event, an acknowledgement, or a stale read dropped."""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.lane_work import ModelLaneWorkEvent
from pydantic import BaseModel, ConfigDict, model_validator


class ModelLaneWorkClassifyAnswer(BaseModel):
    """Only ``outcome == "event"`` carries an event; the other two fold nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["event", "acknowledged", "dropped"]
    event: ModelLaneWorkEvent | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelLaneWorkClassifyAnswer:
        if (self.outcome == "event") != (self.event is not None):
            msg = "an event outcome carries an event and no other outcome does"
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelLaneWorkClassifyAnswer"]
