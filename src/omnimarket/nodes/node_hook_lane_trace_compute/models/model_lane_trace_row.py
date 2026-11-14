# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLaneTraceRow(BaseModel):
    """One completed call of a lane as the lane trace query returns it: the PostToolUse event joined to the
    chunks of its stored output (session_content), concatenated in chunk order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    emitted_at: str
    session_id: str
    tool_use_id: str
    tool_name: str
    hook_event_name: Literal["PostToolUse", "PostToolUseFailure"]
    exit_code: int | None = None
    lane: str
    content_kind: Literal["tool_response", "tool_error"] | None = None
    output: str | None = None
