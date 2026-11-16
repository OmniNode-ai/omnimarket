# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the J11 join (OMN-22961, rebuild plan 4.1, piece 11 to piece 12).

A runtime-affecting merge's PASS lab receipt is consumed by dispatch, and the lane starts from the immutable dispatch
record. No PASS receipt for the exact merged sha on the required lane means nothing starts. Every code lives in the
contract's ``join_j11`` section; nothing here has a default value.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_lane_dispatch_effect.models.model_lane_dispatch_effect import (
    ModelLaneDispatchItemResult,
)


class EnumJoinJ11Outcome(StrEnum):
    """What the J11 join reads for one merge."""

    STARTED = "started"
    NOT_STARTED = "not_started"
    EXEMPT = "exempt"


class ModelJoinJ11Policy(BaseModel):
    """The contract's ``join_j11`` section: the two piece numbers and the typed reasons nothing started."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    receipt_missing_code: str = Field(min_length=1)
    receipt_failed_code: str = Field(min_length=1)
    receipts_unreadable_code: str = Field(min_length=1)
    dispatch_not_placed_code: str = Field(min_length=1)


class ModelJoinJ11Request(BaseModel):
    """One merge, the lane it needs a PASS receipt from, and the one dispatch spec to send once the receipt reads PASS."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    merged_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merged_at: AwareDatetime
    judged_at: AwareDatetime
    runtime_affecting: bool
    required_lane: str = Field(min_length=1)
    record_json: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.judged_at < self.merged_at:
            raise ValueError("judged_at is before merged_at")
        return self


class ModelJoinJ11Result(BaseModel):
    """The join's reading: whether a lane started, why not when it did not, and the dispatch outcome behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    merged_sha: str
    outcome: EnumJoinJ11Outcome
    reason_code: str | None = None
    detail: str = ""
    dispatch: ModelLaneDispatchItemResult | None = None


__all__: list[str] = [
    "EnumJoinJ11Outcome",
    "ModelJoinJ11Policy",
    "ModelJoinJ11Request",
    "ModelJoinJ11Result",
]
