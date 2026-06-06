# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_changelog_audit_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelChangelogEntry(BaseModel):
    """A single classified changelog entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Repository the entry belongs to")
    version: str = Field(description="Release version tag")
    date: str = Field(description="ISO-8601 release date")
    entry_type: str = Field(
        description="Classification: breaking | feature | fix | chore | unknown"
    )
    description: str = Field(description="Changelog entry text")
    affects_dependencies: list[str] = Field(
        default_factory=list,
        description="Dependency packages mentioned in this entry",
    )


class ModelChangelogAuditResult(BaseModel):
    """Result of the changelog audit computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ModelChangelogEntry] = Field(
        default_factory=list,
        description="Classified changelog entries",
    )
    summary: dict[str, int] = Field(
        default_factory=dict,
        description="Aggregated counts by entry type (breaking, feature, fix, chore)",
    )
