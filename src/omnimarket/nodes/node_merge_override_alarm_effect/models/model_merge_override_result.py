# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one handled input did; the handler's return."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_override_alarm_effect.models.enum_merge_override_outcome import (
    EnumMergeOverrideOutcome,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models.model_merge_override_verdict_event import (
    ModelMergeOverrideVerdictEvent,
)


class ModelMergeOverrideResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumMergeOverrideOutcome
    reason: str | None = None
    verdict: ModelMergeOverrideVerdictEvent | None = None
    finding_published: bool = False
    command_published: bool = False
    swept: int = Field(default=0, ge=0)
    error: str | None = None
