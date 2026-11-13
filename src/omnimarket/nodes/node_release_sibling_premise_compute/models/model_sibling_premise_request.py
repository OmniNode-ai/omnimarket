# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input to one sibling premise decision."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    ModelDevMergeRedeployReceipt,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ModelSiblingPremiseRequest(BaseModel):
    """One release-train premise question: does the dev lane run candidate revision X?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=3)
    candidate_sha: str
    candidate_merged_at: Annotated[datetime, AwareDatetime]
    descendant_shas: tuple[str, ...] = ()
    receipts: tuple[ModelDevMergeRedeployReceipt, ...]
    as_of: Annotated[datetime, AwareDatetime]
    pending_window_seconds: int = Field(ge=1)

    @field_validator("candidate_sha")
    @classmethod
    def _validate_candidate_sha(cls, v: str) -> str:
        if not _SHA_RE.match(v):
            msg = "candidate_sha must be exactly 40 lowercase hex characters"
            raise ValueError(msg)
        return v

    @field_validator("descendant_shas")
    @classmethod
    def _validate_descendant_shas(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for sha in v:
            if not _SHA_RE.match(sha):
                msg = "each descendant_sha must be exactly 40 lowercase hex characters"
                raise ValueError(msg)
        return v


__all__ = [
    "ModelSiblingPremiseRequest",
]
