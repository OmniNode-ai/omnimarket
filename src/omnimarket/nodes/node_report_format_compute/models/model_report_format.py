# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for node_report_format_compute.

All models are frozen and pure-data — no I/O, no LLM calls, no side effects.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelReportFormatOutput(BaseModel):
    """Output envelope for the report format compute handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    blocks: list[dict[str, Any]] = Field(
        description=(
            "Slack Block Kit blocks array (max 50 elements). "
            "Ready to pass to chat.postMessage as the blocks field."
        )
    )
    fallback_text: str = Field(
        description=(
            "mrkdwn fallback text for notifications and accessibility. "
            "Used as the text field in chat.postMessage when blocks is not supported."
        )
    )
    truncated: bool = Field(
        description=(
            "True when the source content was truncated to fit Block Kit limits. "
            "A 'full report' link-out block is appended when true."
        )
    )
    block_count: int = Field(ge=0, description="Number of blocks in the output array.")
    section_count: int = Field(ge=0, description="Number of content sections rendered.")
