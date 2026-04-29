# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch-scoped state record for ``node_dispatch_worker``."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchRecord(BaseModel):
    """Per-dispatch metadata snapshot keyed by ``agent_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]{1,64}$",
    )
    dispatched_at: datetime
    dispatcher: str = Field(..., min_length=1)
    ticket: str = Field(..., min_length=1, max_length=128)
    allowed_tools: list[str] = Field(default_factory=list)
    prompt_digest: str = Field(..., min_length=1)
    parent_session_id: str = Field(..., min_length=1)


__all__: list[str] = ["ModelDispatchRecord"]
