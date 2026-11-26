# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request/result of the agent log -> OTLP GenAI trace conversion (OMN-24941).

The conversion is pure: one headless agent run's parsed stream-json lines go in, and an
OTLP/JSON TracesData document plus an MCP-style tools list come out. Every id, time and
byte of output is derived from the records themselves, so one log converts twice to
byte-identical results.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelAgentTraceConvertRequest(_Frozen):
    """One headless agent run: the lane it ran in and its parsed log lines."""

    lane: str
    records: tuple[dict[str, Any], ...]
    max_text_chars: int = Field(default=20_000, ge=0)


class ModelAgentTraceConvertResult(_Frozen):
    """The OTLP trace of the run, the tools it used and the tallies of both."""

    otlp: dict[str, Any]
    tools: dict[str, Any]
    tool_call_count: int
    tool_result_count: int
    unmatched_tool_call_ids: tuple[str, ...]
    final_message: str
