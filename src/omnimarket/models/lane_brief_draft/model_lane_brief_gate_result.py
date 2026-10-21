# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict on a draft and the problems split by who can fix them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_brief_draft.enum_brief_gate_verdict import (
    EnumBriefGateVerdict,
)


class ModelLaneBriefGateResult(BaseModel):
    """``dispatchable`` is true only for ACCEPTED. ``defect_suspected`` marks a shell part the
    checker refused again after code regenerated it, which the model cannot have caused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumBriefGateVerdict
    dispatchable: bool
    shell_problems: tuple[str, ...] = ()
    free_text_problems: tuple[str, ...] = ()
    defect_suspected: bool = False
