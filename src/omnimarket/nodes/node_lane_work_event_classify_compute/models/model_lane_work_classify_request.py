# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One raw or typed result to classify against a workflow's state."""

from __future__ import annotations

from typing import Any

from omnibase_core.models.lane_work import ModelLaneWorkState
from pydantic import BaseModel, ConfigDict, Field, model_validator

RAW_TEXT = "raw_text"


class ModelLaneWorkClassifyRequest(BaseModel):
    """``result_kind`` is ``raw_text`` (with ``raw_result``) or a typed result kind (with ``payload``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelLaneWorkState
    result_kind: str = Field(..., min_length=1)
    raw_result: str | None = None
    payload: dict[str, Any] | None = None
    input_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _owed(self) -> ModelLaneWorkClassifyRequest:
        if self.result_kind == RAW_TEXT:
            if self.raw_result is None:
                msg = "raw_text requires raw_result"
                raise ValueError(msg)
        elif self.payload is None:
            msg = f"typed result {self.result_kind!r} requires payload"
            raise ValueError(msg)
        return self


__all__: list[str] = ["RAW_TEXT", "ModelLaneWorkClassifyRequest"]
