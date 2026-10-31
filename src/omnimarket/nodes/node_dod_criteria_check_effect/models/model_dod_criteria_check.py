# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one trusted criteria check of a pull request head (OMN-21676)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dod_criteria_check_effect.models.enum_dod_criteria_verdict import (
    EnumDodCriteriaVerdict,
)
from omnimarket.nodes.node_dod_verify.models.model_criteria_drift import (
    ModelCriteriaAmendment,
    ModelCriterionDrift,
)


class ModelDodCriteriaCheckRequest(BaseModel):
    """Check the criteria of the tickets one pull request head cites."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str = Field(description="owner/name")
    pr_number: int = Field(ge=1)
    head_sha: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="The head the caller is about to merge. A pull request that has moved "
        "past it is unreadable, never checked at the new head.",
    )


class ModelDodCriteriaTicketVerdict(BaseModel):
    """The verdict of one ticket a pull request head cites."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(min_length=1)
    contract_path: str = Field(min_length=1)
    verdict: EnumDodCriteriaVerdict
    detail: str = Field(
        default="",
        description="Why, in a sentence. Never carries a credential.",
    )
    criteria_revision: str | None = None
    drift: tuple[ModelCriterionDrift, ...] = ()
    amendment: ModelCriteriaAmendment | None = None


class ModelDodCriteriaCheckResult(BaseModel):
    """What one check read and concluded; ``verdict`` is the worst of ``tickets``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str
    pr_number: int
    head_sha: str
    verdict: EnumDodCriteriaVerdict
    merge_blocked: bool = Field(
        description="True for drift and for anything unreadable: the caller must not merge."
    )
    reason: str = Field(
        default="", description="The sentence a caller records on a refusal."
    )
    tickets: tuple[ModelDodCriteriaTicketVerdict, ...] = ()


__all__ = [
    "ModelDodCriteriaCheckRequest",
    "ModelDodCriteriaCheckResult",
    "ModelDodCriteriaTicketVerdict",
]
