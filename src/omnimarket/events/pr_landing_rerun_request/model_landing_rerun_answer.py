# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed answer to a rerun request: the verdict, who asked and why (OMN-23830)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing_rerun_request.enum_rerun_verdict import (
    EnumRerunVerdict,
)
from omnimarket.models.landing_decision import PR_KEY_PATTERN, SHA_PATTERN


class ModelLandingRerunAnswer(BaseModel):
    """The one answer to one request. It records the requester and the reason, as the ticket promises."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr: str = Field(pattern=PR_KEY_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN, description="The head requested.")
    job: str = Field(min_length=1)
    requester: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    verdict: EnumRerunVerdict
    reruns_on_head: int = Field(
        ge=0,
        description="Re-runs already granted for this head, this answer's included.",
    )
    limit: int = Field(ge=0, description="The per-head limit read from the overlay.")
    current_head_sha: str | None = Field(
        default=None,
        pattern=SHA_PATTERN,
        description="head_moved: the head the tick reads now.",
    )
    would_rerun: bool = Field(
        default=False,
        description="Observe and replay mode: the verdict is rerun and no write was sent.",
    )
    written: bool = Field(
        default=False,
        description="Act mode: the re-run went out through the GitHub landing effect's writer.",
    )
    detail: str = ""


__all__: list[str] = ["ModelLandingRerunAnswer"]
