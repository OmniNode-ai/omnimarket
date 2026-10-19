# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, seams and events of the Claude review batch (OMN-20910).

The voter (model, endpoint, key reference, provider) arrives in the request,
resolved by the caller from the review voters overlay: this node names none.
A key is a reference, never a value.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumBatchVerdict(StrEnum):
    BLOCKED = "blocked"
    PASSED = "passed"
    DEGRADED = "degraded"


class ModelClaudeReviewBatchRequest(BaseModel):
    """Review replay items from a frozen manifest through admission and a transport."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    item_ids: tuple[str, ...] = Field(
        default=(), description="A subset of the manifest; empty means every item."
    )
    model_id: str = Field(min_length=1)
    endpoint_ref: str = Field(min_length=1)
    secret_ref: str | None = Field(
        default=None,
        description="A key reference; none for an unauthenticated lab voter.",
    )
    provider: str = Field(min_length=1)
    model_tier: str = Field(
        min_length=1, description="The tier the voter's overlay entry declares."
    )
    temperature: float = Field(
        default=1.0, ge=0, description="Claude 4.7 and later refuse a value below 1."
    )
    system_prompt: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)
    cloud_refused_repositories: tuple[str, ...] = Field(
        default=(),
        description="Repositories whose diffs never go to a cloud model; from a private overlay.",
    )
    resume_batch_id: str | None = Field(
        default=None,
        description="Collect a batch submitted earlier instead of submitting again.",
    )
    max_polls: int = Field(default=3, ge=1)
    poll_interval_seconds: float = Field(default=30.0, ge=0)

    @model_validator(mode="after")
    def _no_duplicate_items(self) -> ModelClaudeReviewBatchRequest:
        if len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("an item id appears twice")
        return self


class ModelBatchUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelBatchCall(BaseModel):
    """One review call to place; in memory only, never published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    model_id: str
    endpoint_ref: str
    secret_ref: str | None
    provider: str
    model_tier: str
    temperature: float
    system_prompt: str
    prompt: str
    max_tokens: int


class ModelBatchItemResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    success: bool
    content: str | None = None
    stop_reason: str | None = None
    failure: str | None = None
    usage: ModelBatchUsage = Field(default_factory=ModelBatchUsage)


class ModelBatchPoll(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ended: bool
    results: tuple[ModelBatchItemResult, ...] = ()


class ModelReviewAdmissionRequest(BaseModel):
    """What the budget reducer is asked before a call or batch item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    model_id: str
    worst_case_input_tokens: int = Field(ge=0)
    max_tokens: int = Field(ge=1)
    batch: bool


class ModelReviewAdmissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    admitted: bool
    reason: str
    reservation_id: str | None = None


class ModelReviewCallAdmissionDecided(BaseModel):
    """Event: one admission decision, published before the call it admits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    item_id: str
    model_id: str
    execution_mode: str
    admitted: bool
    reason: str
    reservation_id: str | None


class ModelReviewBatchItemCost(BaseModel):
    """Event: the usage of one item's call, once per item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    item_id: str
    model_id: str
    execution_mode: str
    usage: ModelBatchUsage
    stop_reason: str | None
    outcome: Literal["ok", "truncated", "schema_invalid", "upstream_error"]


class ModelReviewBatchItemVerdict(BaseModel):
    """Event: the verdict on one replay item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    item_id: str
    repo: str
    pr_number: int
    verdict: EnumBatchVerdict
    findings: int = Field(ge=0)
    blocking_findings: int = Field(ge=0)
    execution_mode: str
    reason: str | None = None


class ModelClaudeReviewBatchResult(BaseModel):
    """Terminal event: the batch collected, or still running with its id to resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "in_progress"]
    batch_id: str | None
    execution_mode: str
    submitted: int
    refused: int
    verdicts: tuple[ModelReviewBatchItemVerdict, ...]
    pending_item_ids: tuple[str, ...] = Field(
        default=(),
        description="Items submitted and not yet answered; pass them with resume_batch_id.",
    )


__all__ = [
    "EnumBatchVerdict",
    "ModelBatchCall",
    "ModelBatchItemResult",
    "ModelBatchPoll",
    "ModelBatchUsage",
    "ModelClaudeReviewBatchRequest",
    "ModelClaudeReviewBatchResult",
    "ModelReviewAdmissionDecision",
    "ModelReviewAdmissionRequest",
    "ModelReviewBatchItemCost",
    "ModelReviewBatchItemVerdict",
    "ModelReviewCallAdmissionDecided",
]
