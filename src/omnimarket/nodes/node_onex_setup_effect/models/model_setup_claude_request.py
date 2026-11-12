# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Owned Claude component selection and requested effect."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .model_setup_plan import ModelSetupPlan


class ModelSetupClaudeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    component: Literal["claude-plugin", "claude-hooks"]
    operation: Literal["observe", "apply"] = "observe"
    plan: ModelSetupPlan
