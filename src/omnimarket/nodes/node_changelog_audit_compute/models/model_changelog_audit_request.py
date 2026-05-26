# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_changelog_audit_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelChangelogAuditRequest(BaseModel):
    """Request to audit changelogs for a set of repositories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: list[str] = Field(description="Repository names or paths to audit")
    since_date: str = Field(
        description="ISO-8601 date string; only entries on or after this date are returned"
    )
    dependencies: list[str] | None = Field(
        default=None,
        description="Optional dependency filter — only include entries that affect these packages",
    )
