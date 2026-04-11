# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Request model for agent learning retrieval."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRetrievalMatchType(StrEnum):
    """How to match against the learning store."""

    ERROR_SIGNATURE = "error_signature"
    TASK_CONTEXT = "task_context"
    AUTO = "auto"


class ModelAgentLearningRetrievalRequest(BaseModel):
    """Request to retrieve relevant agent learnings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_type: EnumRetrievalMatchType = Field(
        default=EnumRetrievalMatchType.AUTO,
        description="Match strategy",
    )
    error_text: str | None = Field(
        default=None,
        max_length=4000,
        description="Error message for error_signature matching",
    )
    repo: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Repository to scope search to",
    )
    file_paths: tuple[str, ...] = Field(
        default=(),
        description="File paths for context matching",
    )
    task_type: str | None = Field(
        default=None,
        max_length=64,
        description="Optional task type filter",
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum results to return",
    )
    min_similarity_error: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity for error signature matches",
    )
    min_similarity_context: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity for task context matches",
    )
