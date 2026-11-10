# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the J10 join (OMN-22957, rebuild plan 4.1, piece 10 to piece 11).

A runtime-affecting merge yields a PASS lab receipt keyed by its sha, and the running runtime reads back that exact
sha and answers one correlated functional request. A PASS receipt beside a stale or broken runtime fails the join.
Every threshold lives in the contract's ``join_j10`` section, in the request (the deadline, from the piece's overlay)
or in the overlay model below; nothing here has a default value.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
)

_SHA = re.compile(r"[0-9a-f]{40}")


class EnumJoinJ10Outcome(StrEnum):
    """What the J10 join reads for one merge."""

    JOINED = "joined"
    FAILED = "failed"
    PENDING = "pending"
    EXEMPT = "exempt"


class ModelJoinJ10Policy(BaseModel):
    """The contract's ``join_j10`` section: the two piece numbers, the emitting node and the typed failure codes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    emission_node: str = Field(min_length=1)
    receipt_missing_code: str = Field(min_length=1)
    receipt_late_code: str = Field(min_length=1)
    receipt_failed_code: str = Field(min_length=1)
    receipts_unreadable_code: str = Field(min_length=1)
    runtime_stale_code: str = Field(min_length=1)
    runtime_broken_code: str = Field(min_length=1)
    runtime_uncorrelated_code: str = Field(min_length=1)


class ModelJoinJ10Request(BaseModel):
    """One merge, when it merged, the lane and deadline that judge it, and the time the join is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    merged_sha: str
    merged_at: AwareDatetime
    judged_at: AwareDatetime
    runtime_affecting: bool
    required_lane: str = Field(min_length=1)
    deadline_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not _SHA.fullmatch(self.merged_sha):
            raise ValueError(
                f"not an exact sha (40 lowercase hex): {self.merged_sha!r}"
            )
        return self


class ModelReceiptRead(BaseModel):
    """The lab pass receipts found for one sha, or why they could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipts: tuple[ModelLabPassReceiptFact, ...]
    unread_reason: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.unread_reason is not None and not self.unread_reason.strip():
            raise ValueError("unread_reason names no reason")
        return self


class ModelRuntimeAsk(BaseModel):
    """The one correlated functional request: the correlation id to echo and the sha the runtime must read back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    expected_sha: str


class ModelRuntimeAnswer(BaseModel):
    """What the running runtime answered: the sha it reports, the correlation id it echoed and whether it worked.

    ``reachable`` is false when no answer came at all (refused, timed out, not JSON); ``detail`` says why.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: bool
    observed_sha: str | None
    echoed_correlation_id: str | None
    functional_ok: bool
    detail: str


class ModelJoinJ10Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumJoinJ10Outcome
    correlation_id: str
    merged_sha: str
    deadline_at: AwareDatetime
    failure_code: str | None = None
    failure_detail: str = ""
    runtime_observed_sha: str | None = None


class ModelDevDeployJoinOverlay(BaseModel):
    """The real-backend settings of the J10 join: where the receipts and the runtime are read. No default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipts_url: str = Field(min_length=1)
    receipt_lane: str = Field(min_length=1)
    receipt_pass_word: str = Field(min_length=1)
    runtime_url: str = Field(min_length=1)
    runtime_correlation_header: str = Field(min_length=1)
    runtime_sha_key: str = Field(min_length=1)
    runtime_correlation_key: str = Field(min_length=1)
    runtime_ok_key: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    backend: Literal["in_memory", "real"]


__all__: list[str] = [
    "EnumJoinJ10Outcome",
    "ModelDevDeployJoinOverlay",
    "ModelJoinJ10Policy",
    "ModelJoinJ10Request",
    "ModelJoinJ10Result",
    "ModelReceiptRead",
    "ModelRuntimeAnswer",
    "ModelRuntimeAsk",
]
