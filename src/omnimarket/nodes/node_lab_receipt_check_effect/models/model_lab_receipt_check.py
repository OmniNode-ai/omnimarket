# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one lab receipt check of a pull request head (OMN-21826)."""

from __future__ import annotations

from uuid import UUID

from omnibase_core.enums.enum_lab_receipt_phase import EnumLabReceiptPhase
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lab_receipt_check_effect.models.enum_lab_receipt_verdict import (
    EnumLabReceiptVerdict,
)


class ModelLabReceiptCheckRequest(BaseModel):
    """Check the lab receipt of one pull request head in one phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str = Field(description="owner/name")
    pr_number: int = Field(ge=1)
    head_sha: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="The head the caller is about to merge. A pull request that has moved "
        "past it is unreadable, never checked at the new head.",
    )
    phase: EnumLabReceiptPhase = Field(
        description="present reads what the repository token can read; verify also reads the lab."
    )


class ModelLabReceiptIssueView(BaseModel):
    """One issue of the core validator, as the caller records it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, description="The rule id of the core contract row.")
    severity: str = Field(min_length=1)
    message: str


class ModelLabReceiptCheckResult(BaseModel):
    """What one check read and concluded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str
    pr_number: int
    head_sha: str
    phase: EnumLabReceiptPhase
    verdict: EnumLabReceiptVerdict
    merge_blocked: bool = Field(
        description="True for a refusal and for anything unreadable: the caller must not merge."
    )
    required: bool = Field(
        default=False, description="Whether the validator found a change that runs."
    )
    classes: tuple[str, ...] = Field(
        default=(),
        description="The classes of the counted files that made a receipt required.",
    )
    issues: tuple[ModelLabReceiptIssueView, ...] = ()
    reason: str = Field(
        default="", description="The sentence a caller records on a refusal."
    )


__all__ = [
    "ModelLabReceiptCheckRequest",
    "ModelLabReceiptCheckResult",
    "ModelLabReceiptIssueView",
]
