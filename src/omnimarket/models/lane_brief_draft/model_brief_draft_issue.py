# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One defect of the drafted slots."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_brief_draft.enum_brief_draft_refusal import (
    EnumBriefDraftRefusal,
)


class ModelBriefDraftIssue(BaseModel):
    """``slot`` is empty when the defect is about the whole draft; ``detail`` quotes the offender."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal: EnumBriefDraftRefusal
    slot: str = ""
    detail: str = ""
