# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which review a provider call belongs to (OMN-20910).

The gateway copies this from the review's request metadata onto the delegation
call, so the effect that makes the provider attempt can say whose attempt it
was. Nothing here is a secret.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_review_call_attempt_kind import EnumReviewCallAttemptKind
from omnimarket.enums.enum_review_execution_mode import EnumReviewExecutionMode


class ModelReviewCallProvenance(BaseModel):
    """The review run, pull request and voter one provider call was made for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_run_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    voter_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    chunk_index: int = Field(default=0, ge=0)
    chunk_count: int = Field(default=1, ge=1)
    attempt_kind: EnumReviewCallAttemptKind = EnumReviewCallAttemptKind.FIRST
    execution_mode: EnumReviewExecutionMode = EnumReviewExecutionMode.SYNCHRONOUS
    catalog_version: str = ""
    prompt_version: str = ""


__all__ = ["ModelReviewCallProvenance"]
