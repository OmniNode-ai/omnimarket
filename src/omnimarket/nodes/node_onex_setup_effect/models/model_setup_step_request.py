# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed invocation of an owned setup component effect."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .model_setup_plan import ModelSetupPlan


class ModelSetupStepRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step: Literal["workspace", "user_config", "runtime_config", "identity", "models"]
    operation: Literal["observe", "apply"] = "observe"
    plan: ModelSetupPlan
