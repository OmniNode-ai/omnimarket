# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The assembled brief, or the defects of the draft; the checker has not yet seen either."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_brief_draft.model_brief_draft_issue import (
    ModelBriefDraftIssue,
)


class ModelLaneBriefAssembleResult(BaseModel):
    """``brief`` is empty unless ``assembled``; assembling is not accepting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assembled: bool
    brief: str = ""
    brief_sha256: str = ""
    issues: tuple[ModelBriefDraftIssue, ...] = ()
