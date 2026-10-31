# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O of the receipt-gate pin advance effect (OMN-20134)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_receipt_gate_pin_advance import (
    EnumCallerVerdict,
)

DEV_TAG = r"^dev-\d{14}-[0-9a-f]{12}$"


class ModelDevMergeTagEvent(BaseModel):
    """A dev-merge tag created on a repository: the event that triggers the advance.

    Published on ``onex.evt.github.dev-merge-tag.v1`` by the GitHub webhook
    ingress when a ``dev-<UTC14>-<sha12>`` tag is pushed. The effect advances the
    callers only for the tag of the policy's source repository. Fields a newer
    producer adds are ignored.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    tag: str = Field(pattern=DEV_TAG)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    delivery_id: UUID | None = None
    as_of: datetime | None = None
    requested_by_lane: str = Field(default="receipt-gate-pin-advance", min_length=1)
    dry_run: bool = Field(
        default=False,
        description="Plan and report; read nothing writable, open no branch or pull request.",
    )


class EnumPinPrOutcome(StrEnum):
    """What happened at one caller that needed a pull request."""

    OPENED = "opened"
    REPLAYED = "replayed"
    DRY_RUN = "dry_run"
    FAILED = "failed"


class EnumPinAdvanceDisposition(StrEnum):
    """What the triggering tag led to."""

    ADVANCED = "advanced"
    NOTHING_STALE = "nothing_stale"
    IGNORED = "ignored"


class ModelPinPrResult(BaseModel):
    """One caller's pin pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    verdict: EnumCallerVerdict
    outcome: EnumPinPrOutcome
    target_tag: str
    branch: str
    branch_head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    detail: str = ""


class ModelPinUnjudged(BaseModel):
    """A caller the planner could not judge: declared data and repository disagree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    verdict: EnumCallerVerdict
    detail: str


class ModelReceiptGatePinAdvanceCompleted(BaseModel):
    """The terminal event: one result per caller that needed a pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_tag: str
    disposition: EnumPinAdvanceDisposition
    newest_tag: str | None = None
    results: tuple[ModelPinPrResult, ...] = ()
    unjudged: tuple[ModelPinUnjudged, ...] = ()
    detail: str = ""

    @property
    def failed(self) -> tuple[ModelPinPrResult, ...]:
        return tuple(r for r in self.results if r.outcome is EnumPinPrOutcome.FAILED)


__all__ = [
    "DEV_TAG",
    "EnumPinAdvanceDisposition",
    "EnumPinPrOutcome",
    "ModelDevMergeTagEvent",
    "ModelPinPrResult",
    "ModelPinUnjudged",
    "ModelReceiptGatePinAdvanceCompleted",
]
