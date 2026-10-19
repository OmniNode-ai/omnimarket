# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded-finding envelope (OMN-21188).

:class:`ModelRecordedFinding` wraps the canonical
:class:`~omnimarket.models.model_review_finding.ModelReviewFinding` without
changing it. The wrapped finding is held whole, so converting into the envelope
and back loses nothing; the envelope adds only what a record needs beyond the
finding: a ``finding_id`` unique to this emission, the two identity keys, the
review thread the finding was posted as, and a reference to the finding in the
review it came from.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_review_execution_status import EnumReviewThreadKind
from omnimarket.models.model_review_finding import ModelReviewFinding


class ModelFindingThreadRef(BaseModel):
    """The review thread or general comment a finding was posted as."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str = Field(min_length=1)
    kind: EnumReviewThreadKind


class ModelOriginalFindingRef(BaseModel):
    """The finding in the review it was first raised in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID
    review_id: UUID | None = Field(
        default=None, description="The review the finding came from, when known."
    )


class ModelRecordedFinding(BaseModel):
    """A review finding as one record emits it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID = Field(description="Unique per emission; not the finding's id.")
    finding: ModelReviewFinding
    defect_key: str | None = Field(
        default=None,
        description="Shared across arms only through an explicit equivalence edge.",
    )
    occurrence_key: str | None = Field(
        default=None, description="Set by the finding identity node; None until then."
    )
    thread_ref: ModelFindingThreadRef | None = Field(
        default=None, description="Set only where a review thread was posted."
    )
    original_finding_ref: ModelOriginalFindingRef | None = None

    @classmethod
    def from_finding(
        cls,
        finding: ModelReviewFinding,
        *,
        finding_id: UUID,
        defect_key: str | None = None,
        occurrence_key: str | None = None,
        thread_ref: ModelFindingThreadRef | None = None,
        original_finding_ref: ModelOriginalFindingRef | None = None,
    ) -> Self:
        return cls(
            finding_id=finding_id,
            finding=finding,
            defect_key=defect_key,
            occurrence_key=occurrence_key,
            thread_ref=thread_ref,
            original_finding_ref=original_finding_ref,
        )

    def to_finding(self) -> ModelReviewFinding:
        return self.finding


__all__: list[str] = [
    "ModelFindingThreadRef",
    "ModelOriginalFindingRef",
    "ModelRecordedFinding",
]
