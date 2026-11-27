# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One input to the fold: a typed event, or a result the classifier names first (OMN-26721)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from omnibase_core.models.lane_work import ModelLaneWorkEvent
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLaneWorkIngress(BaseModel):
    """``event`` (typed) or ``result_kind`` with ``raw_result`` or ``payload`` (a result to classify).

    ``correlation_id`` is the caller's: the step reply carries it back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    event: ModelLaneWorkEvent | None = None
    workflow_id: str | None = None
    result_kind: str | None = Field(default=None, min_length=1)
    raw_result: str | None = None
    payload: dict[str, Any] | None = None
    input_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_form(self) -> ModelLaneWorkIngress:
        if (self.event is None) == (self.result_kind is None):
            msg = "an ingress input carries an event or a result_kind, not both and not neither"
            raise ValueError(msg)
        if self.result_kind is not None and (
            self.workflow_id is None or self.input_id is None
        ):
            msg = "a result to classify names its workflow_id and a stable input_id"
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelLaneWorkIngress"]
