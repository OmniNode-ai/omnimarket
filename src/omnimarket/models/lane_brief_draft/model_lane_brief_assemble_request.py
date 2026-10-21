# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model's drafted slots, the fact table they were drafted from and the shell parts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_brief_draft.model_brief_draft_slot import (
    ModelBriefDraftSlot,
)
from omnimarket.models.lane_brief_draft.model_brief_shell_parts import (
    ModelBriefShellParts,
)


class ModelLaneBriefAssembleRequest(BaseModel):
    """``fact_table`` is the text the model was given; an identifier it states must occur there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    fact_table: str
    drafted_slots: tuple[ModelBriefDraftSlot, ...]
    shell: ModelBriefShellParts
