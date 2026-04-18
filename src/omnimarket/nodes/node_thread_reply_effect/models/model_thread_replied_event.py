# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelThreadRepliedEvent -- output contract for node_thread_reply_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelThreadRepliedEvent(BaseModel):
    """Output emitted after attempting to post a PR thread reply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Correlation ID from the input.")
    pr_number: int = Field(..., description="PR number acted upon.")
    repo: str = Field(..., description="GitHub repo slug (org/repo).")
    comment_id: str | None = Field(
        default=None,
        description="GitHub comment ID of the posted reply, or None if not posted.",
    )
    reply_posted: bool = Field(
        ..., description="Whether a reply was successfully posted."
    )
    is_draft: bool = Field(
        ...,
        description="True if posted as a draft comment tagged <!-- omni-draft -->.",
    )
    used_fallback: bool = Field(
        default=False,
        description="True if the fallback LLM model was used.",
    )


__all__: list[str] = ["ModelThreadRepliedEvent"]
