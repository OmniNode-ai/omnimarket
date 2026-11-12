# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gateway hook registration carries executable identity, never credentials."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelGatewayHookInstallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Literal["install", "status", "uninstall"]


class ModelGatewayHookInstallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["success", "failure", "action_required"]
    changed: bool = False
    registered: bool = False
    fix: str | None = None


class ModelGatewayHookOwnership(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    phase: Literal["enabling", "enabled", "disabled"]
    command: str = Field(min_length=1, max_length=4096)
    backup_name: str = Field(
        pattern=r"^claude_gateway_hooks\.settings\.[0-9a-f]{32}\.backup$"
    )


class ModelGatewayHookProfileRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event: Literal["UserPromptSubmit", "PreToolUse"]
    matcher: str | None = None
    timeout_seconds: int = Field(ge=1, le=30)
    on_failure: Literal["block"]
