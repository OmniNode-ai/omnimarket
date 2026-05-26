# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_dep_cascade_dedup_orchestrator [OMN-12213].

ModelDepCascadeDedupResult: aggregated outcome emitted after all repos are
processed.

Supporting models:
- EnumPRAction: action taken on a PR (CLOSED / KEPT / SKIPPED)
- ModelPRRecord: per-PR record in the dedup report
- ModelPackageGroup: a (repo, package) group with one keeper and N superseded PRs
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPRAction(StrEnum):
    """Action taken on a PR during dedup processing."""

    CLOSED = "closed"
    KEPT = "kept"
    SKIPPED = "skipped"


class ModelPRRecord(BaseModel):
    """Per-PR record in the dedup report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Repo in 'owner/name' form.")
    pr_number: int = Field(description="PR number.")
    package: str = Field(description="Package name extracted from the PR title.")
    target_version: str = Field(
        default="",
        description="Target package version parsed from the PR title.",
    )
    action: EnumPRAction = Field(description="Action taken on this PR.")
    superseded_by: int = Field(
        default=0,
        description=(
            "PR number of the keeper that supersedes this PR. "
            "0 when action != CLOSED or when already-on-main supersedes."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable reason for the action (e.g. 'already on main').",
    )


class ModelPackageGroup(BaseModel):
    """A (repo, package) group with one keeper and N superseded PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Repo in 'owner/name' form.")
    package: str = Field(description="Package name.")
    keeper_pr_number: int = Field(
        default=0,
        description=(
            "PR number kept as the highest-version or sole remaining PR. "
            "0 when all PRs in the group are superseded by an already-merged bump."
        ),
    )
    superseded_pr_numbers: tuple[int, ...] = Field(
        default=(),
        description="PR numbers identified as superseded within this group.",
    )


class ModelDepCascadeDedupResult(BaseModel):
    """Output of the dep cascade dedup orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos_scanned: int = Field(
        default=0,
        description="Number of repos scanned during this run.",
    )
    groups_found: int = Field(
        default=0,
        description="Number of (repo, package) groups with multiple open dep-bump PRs.",
    )
    prs_closed: int = Field(
        default=0,
        description="Number of superseded PRs closed (0 in dry_run mode).",
    )
    prs_kept: int = Field(
        default=0,
        description="Number of PRs designated as keepers.",
    )
    prs_skipped: int = Field(
        default=0,
        description="Number of PRs skipped (e.g. parse errors, non-dep-bump PRs).",
    )
    dry_run: bool = Field(
        default=False,
        description="True when the run was executed in dry-run mode.",
    )
    package_groups: tuple[ModelPackageGroup, ...] = Field(
        default=(),
        description="Per-group dedup summary, ordered by (repo, package).",
    )
    pr_records: tuple[ModelPRRecord, ...] = Field(
        default=(),
        description="Per-PR action records for the full report table.",
    )
