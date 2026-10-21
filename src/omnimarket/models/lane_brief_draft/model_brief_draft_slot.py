# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One free-text slot as the model drafted it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_brief_draft.enum_brief_draft_slot import EnumBriefDraftSlot


class ModelBriefDraftSlot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: EnumBriefDraftSlot
    text: str
