# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Response model for agent learning retrieval."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_request import (
    EnumRetrievalMatchType,
)
from pydantic import BaseModel, ConfigDict, Field


class EnumRetrievalTaskType(StrEnum):
    """Task type classification for retrieved learnings.

    Mirrors EnumLearningTaskType in omnibase_infra. Both enums MUST remain
    identical — see Canonical Freshness Formula note in the design doc for
    the cross-repo sync contract.
    """

    CI_FIX = "ci_fix"
    MIGRATION = "migration"
    FEATURE = "feature"
    REFACTOR = "refactor"
    BUG_FIX = "bug_fix"
    TEST = "test"
    DOCS = "docs"
    DEPENDENCY = "dependency"
    UNKNOWN = "unknown"


class ModelRetrievedLearning(BaseModel):
    """A single retrieved learning with match metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    learning_id: UUID = Field(..., description="Primary key of the learning record")
    match_type: EnumRetrievalMatchType = Field(
        ..., description="How this match was found"
    )
    similarity: float = Field(..., ge=0.0, le=1.0, description="Cosine similarity")
    freshness_score: float = Field(
        ..., ge=0.0, le=1.0, description="Freshness decay factor"
    )
    combined_score: float = Field(
        ..., ge=0.0, le=1.0, description="Final ranking score"
    )
    repo: str = Field(..., description="Repository name")
    resolution_summary: str = Field(..., description="What the agent did")
    error_signatures: tuple[str, ...] = Field(
        default=(), description="Failed tool summaries encountered"
    )
    file_paths_touched: tuple[str, ...] = Field(
        default=(), description="Files modified"
    )
    ticket_id: str | None = Field(default=None, description="Associated ticket")
    task_type: EnumRetrievalTaskType = Field(..., description="Task classification")
    age_days: int = Field(..., ge=0, description="Days since learning was created")
    created_at: datetime = Field(..., description="When the learning was created")


class ModelAgentLearningRetrievalResponse(BaseModel):
    """Response from agent learning retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches: tuple[ModelRetrievedLearning, ...] = Field(
        default=(),
        description="Matched learnings sorted by combined_score descending",
    )
    query_ms: int = Field(..., ge=0, description="Total query time in milliseconds")
    error_matches_count: int = Field(
        default=0, ge=0, description="Matches from error collection"
    )
    context_matches_count: int = Field(
        default=0, ge=0, description="Matches from context collection"
    )
