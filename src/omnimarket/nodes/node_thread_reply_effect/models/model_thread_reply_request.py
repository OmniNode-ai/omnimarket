# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelThreadReplyRequest -- input contract for node_thread_reply_effect."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelThreadReplyRequest(BaseModel):
    """Request to post an LLM-generated reply to a PR review thread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline correlation ID.")
    pr_number: int = Field(..., description="GitHub PR number.")
    repo: str = Field(..., description="GitHub repo slug (org/repo).")
    thread_body: str = Field(
        ..., description="Full text of the review thread to reply to."
    )
    routing_policy: dict[str, Any] = Field(
        ..., description="Routing policy dict from the command envelope."
    )
    head_ref_name: str | None = Field(
        default=None,
        description="Branch name; the handler refuses a protected branch.",
    )


__all__ = ["ModelThreadReplyRequest"]
