# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The label one run received and why (OMN-19787)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)


class ModelGroundTruthLabel(BaseModel):
    """``rater_role`` names the evidence source, never the gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    source: Literal["hidden_test", "checklist"]
    rater_role: str = Field(min_length=1)
    label: EnumGateEvalLabel
    reason: str = Field(min_length=1)
