# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A gateway hook observes metadata only; it never carries or delegates a prompt."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelClaudeGatewayHookRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)
    hook_event_name: Literal["UserPromptSubmit", "PreToolUse"]
    cwd: Path
    tool_name: str | None = None


class ModelClaudeGatewayHookResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    decision: Literal["allow", "block"]
    reason: str | None = None
