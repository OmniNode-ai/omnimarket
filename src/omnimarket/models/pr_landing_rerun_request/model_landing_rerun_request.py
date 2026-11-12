# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lane's request to re-run one failed job of a pull request on its current head (OMN-23830)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import PR_KEY_PATTERN, SHA_PATTERN


class ModelLandingRerunRequest(BaseModel):
    """The request is a fact the landing tick reads; the tick answers it and never the requester's lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr: str = Field(pattern=PR_KEY_PATTERN, description="owner/repo#n.")
    head_sha: str = Field(
        pattern=SHA_PATTERN, description="The head the requester saw the job fail on."
    )
    job: str = Field(min_length=1, description="The failed check or job name.")
    reason: str = Field(
        min_length=1,
        description="Why a re-run is right, in the requester's words; recorded with the answer.",
    )
    requester: str = Field(min_length=1, description="The lane asking.")


__all__: list[str] = ["ModelLandingRerunRequest"]
