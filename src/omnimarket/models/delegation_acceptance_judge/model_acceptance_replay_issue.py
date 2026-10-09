# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One typed refusal of a committed run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_replay_refusal import (
    EnumAcceptanceReplayRefusal,
)


class ModelAcceptanceReplayIssue(BaseModel):
    """A refusal, with the item it names (empty for a refusal about the whole run)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumAcceptanceReplayRefusal
    item_id: str = ""
    message: str = Field(min_length=1)
