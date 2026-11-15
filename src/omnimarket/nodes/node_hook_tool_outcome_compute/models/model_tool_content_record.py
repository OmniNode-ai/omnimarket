# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inbound record from onex.cmd.omniintelligence.content-captured.v1.

Declares only the fields this node reads. The capture record carries many more
(lane, tool identity, redaction bookkeeping, the content digest); they are
ignored here so a field the producer adds later cannot fail this node.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelToolContentRecord(BaseModel):
    """One content item, or one chunk of it, from full-content hook capture."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(..., description="Claude Code session id.")
    correlation_id: str | None = Field(
        default=None,
        description="Correlation id shared with the hook invocation's other events.",
    )
    content_kind: str = Field(
        ...,
        description="prompt | tool_input | tool_response | tool_error | assistant_reply.",
    )
    tool_name: str | None = Field(
        default=None, description="The tool that produced this content."
    )
    content: str | None = Field(
        default=None, description="Captured content, or this chunk of it."
    )
    chunk_index: int = Field(default=0, ge=0, description="Zero-based chunk index.")
    chunk_count: int = Field(default=1, ge=1, description="Chunks in this item.")
    emitted_at: str | None = Field(
        default=None, description="ISO 8601 emission time of the capture record."
    )
    actor: str | None = Field(
        default=None, description="Capturing agent frontend (claude, codex, ...)."
    )


__all__ = ["ModelToolContentRecord"]
