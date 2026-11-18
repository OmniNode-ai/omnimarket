# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one rerun decision reads: the request, the tick's facts for its pull request, the grants so far (OMN-23830)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import SHA_PATTERN, ModelLandingPrFacts
from omnimarket.models.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)


class ModelRerunGrant(BaseModel):
    """One re-run already granted: the head and the job it was granted for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(pattern=SHA_PATTERN)
    job: str = Field(min_length=1)


class ModelRerunDecideInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelLandingRerunRequest
    current: ModelLandingPrFacts | None = Field(
        description="The tick's facts for the request's pull request; None when it is not open this tick."
    )
    granted: tuple[ModelRerunGrant, ...] = Field(
        default=(), description="Every re-run granted so far, any head."
    )
    max_reruns_per_head: int = Field(
        ge=0, description="The per-head limit, read from the landing overlay."
    )


__all__: list[str] = ["ModelRerunDecideInput", "ModelRerunGrant"]
