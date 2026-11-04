# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic transcript facts with no prompt text."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.work_classification.enum_stream_kind import EnumStreamKind
from omnimarket.models.work_classification.model_source_revision import (
    ModelSourceRevision,
)


class ModelSessionFacts(BaseModel):
    """Aggregate counts and metadata from one transcript stream.

    Timestamps describe the first and last timestamp-bearing records in source
    order; source revisions alone determine whether a read is sufficient.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str | None = None
    stream_kind: EnumStreamKind = EnumStreamKind.SESSION
    agent_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    entrypoint: str | None = None
    effort: str | None = None
    models: tuple[str, ...] = ()
    source_revision: ModelSourceRevision
    first_prompt_sha256: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    record_counts: dict[str, int] = Field(default_factory=dict)
    unknown_record_types: dict[str, int] = Field(default_factory=dict)
    tool_call_counts: dict[str, int] = Field(default_factory=dict)
    permission_decision_counts: dict[str, int] = Field(default_factory=dict)
    sidechain_record_count: int = Field(default=0, ge=0)
    hook_summary_count: int = Field(default=0, ge=0)
    api_error_count: int = Field(default=0, ge=0)
    compact_boundary_count: int = Field(default=0, ge=0)
    cost_state_count: int = Field(default=0, ge=0)
    pr_link_count: int = Field(default=0, ge=0)
    undecodable_line_count: int = Field(default=0, ge=0)
    pr_link_urls: tuple[str, ...] = ()
    written_paths: tuple[str, ...] = ()

    @field_validator("models", "pr_link_urls", "written_paths")
    @classmethod
    def _sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator(
        "record_counts",
        "unknown_record_types",
        "tool_call_counts",
        "permission_decision_counts",
    )
    @classmethod
    def _sorted_keys(cls, value: dict[str, int]) -> dict[str, int]:
        return dict(sorted(value.items()))
