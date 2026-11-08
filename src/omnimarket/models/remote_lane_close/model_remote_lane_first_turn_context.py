# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""First-turn context of a lane by source; an absent measurement is UNKNOWN, never 0."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_remote_lane_receipt_usage import UsageCount


class ModelRemoteLaneFirstTurnContext(BaseModel):
    """What a lane starts with, split by source (OMN-21889).

    ``first_turn_input_tokens`` is ``input_tokens`` of the first assistant usage as
    reported; Claude reports the cached prompt separately, so ``first_turn_cache_read``
    and ``first_turn_cache_write`` are carried beside it and ``first_turn_context_tokens``
    is their sum. ``peak_context_tokens`` is the largest such sum over the assistant
    messages. ``skill_listing_bytes`` is the UTF-8 size of the skill names the init
    event lists (descriptions are not in the log). Brief bytes come from the brief.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    first_turn_input_tokens: UsageCount = "UNKNOWN"
    first_turn_cache_read: UsageCount = "UNKNOWN"
    first_turn_cache_write: UsageCount = "UNKNOWN"
    first_turn_context_tokens: UsageCount = "UNKNOWN"
    brief_bytes: UsageCount = "UNKNOWN"
    skill_listing_count: UsageCount = "UNKNOWN"
    skill_listing_bytes: UsageCount = "UNKNOWN"
    tools_count: UsageCount = "UNKNOWN"
    plugins_count: UsageCount = "UNKNOWN"
    total_turns: UsageCount = "UNKNOWN"
    peak_context_tokens: UsageCount = "UNKNOWN"
