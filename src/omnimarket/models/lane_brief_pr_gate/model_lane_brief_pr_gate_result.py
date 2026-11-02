# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The brief gate's answer for one brief."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_brief.model_lane_brief_issue import ModelLaneBriefIssue
from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_mode import (
    EnumLaneBriefPrGateMode,
)
from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_verdict import (
    EnumLaneBriefPrGateVerdict,
)
from omnimarket.models.wip_gate import ModelWipGateDecision


class ModelLaneBriefPrGateResult(BaseModel):
    """``mode`` is the mode that judged (None when no configuration reached the gate); ``wip`` is the shared rule's decision when it ran."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumLaneBriefPrGateVerdict
    mode: EnumLaneBriefPrGateMode | None = None
    repo: str = ""
    issues: tuple[ModelLaneBriefIssue, ...] = ()
    wip: ModelWipGateDecision | None = None
    line: str
    decided_at: datetime
