# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output of one sibling premise decision."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_release_sibling_premise_compute.models.enum_sibling_premise import (
    EnumSiblingPremiseState,
)


class ModelSiblingPremiseVerdict(BaseModel):
    """The verdict: state, covering merge, lane revision, correlation, receipts considered, detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumSiblingPremiseState
    repo: str
    candidate_sha: str
    covering_merge_sha: str = ""
    lane_revision: str = ""
    correlation_id: UUID | None = None
    receipts_considered: int = Field(ge=0)
    detail: str = Field(max_length=2000)
    as_of: Annotated[datetime, AwareDatetime]

    @property
    def ready(self) -> bool:
        return self.state is EnumSiblingPremiseState.READY


__all__ = [
    "ModelSiblingPremiseVerdict",
]
