# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tool call a Codex lane makes, and what it gets back."""

from pydantic import BaseModel, ConfigDict, Field


class ModelCodexLaneMcpToolCall(BaseModel):
    """A tool name from the fixed list and its string arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, str] = Field(default_factory=dict)


class ModelCodexLaneMcpToolResult(BaseModel):
    """The command's exit code and the tail of its output; `ok` is exit 0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    exit_code: int
    output: str
