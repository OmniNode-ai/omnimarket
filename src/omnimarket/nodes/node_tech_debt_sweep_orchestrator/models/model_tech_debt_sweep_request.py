# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input/output models for node_tech_debt_sweep_orchestrator [OMN-12212].

Contains:
- ModelTechDebtSweepRequest: input to the orchestrator
- ModelCategoryResult: per-category scan summary
- ModelTechDebtSweepResult: output from the orchestrator
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

ALL_CATEGORIES = (
    "type-ignore",
    "noqa",
    "todo-fixme",
    "any-types",
    "skipped-tests",
    "stale-ignores",
)


class ModelTechDebtSweepRequest(BaseModel):
    """Input to the tech debt sweep orchestrator.

    Specifies which repos and categories to scan, and whether to run in dry-run
    mode (report findings without creating tickets).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repos to scan (bare names, e.g. 'omnibase_infra'). "
            "Empty tuple means all Python repos discovered under omni_home root."
        ),
    )
    categories: tuple[str, ...] = Field(
        default=(),
        description=(
            "Tech-debt categories to scan. "
            "Empty tuple means all 6 categories: "
            "type-ignore, noqa, todo-fixme, any-types, skipped-tests, stale-ignores."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="When true, report findings without creating Linear tickets or epics.",
    )
    linear_team: str = Field(
        default="Omninode",
        description="Linear team name to create tickets in.",
    )
    linear_project: str = Field(
        default="Active Sprint",
        description="Linear project name to assign new tickets to.",
    )


class ModelCategoryResult(BaseModel):
    """Per-category scan summary produced by the orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(
        description="Category ID (e.g. 'type-ignore').",
    )
    total_findings: int = Field(
        description="Total findings detected across all scanned repos for this category.",
    )
    new_findings: int = Field(
        description="Net-new findings not already tracked by an open Linear ticket.",
    )
    already_tracked: int = Field(
        description="Findings that matched a dedup key in an existing open ticket.",
    )
    tickets_created: int = Field(
        description="Number of new Linear tickets created for this category.",
    )


class ModelTechDebtSweepResult(BaseModel):
    """Output from the tech debt sweep orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos_scanned: tuple[str, ...] = Field(
        description="Names of repos that were scanned.",
    )
    repos_skipped_stale_ignores: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repos skipped for the stale-ignores category because mypy could not run."
        ),
    )
    category_results: tuple[ModelCategoryResult, ...] = Field(
        description="Per-category scan summaries.",
    )
    total_findings: int = Field(
        description="Sum of all findings across all categories and repos.",
    )
    total_new_findings: int = Field(
        description="Sum of net-new findings across all categories and repos.",
    )
    total_tickets_created: int = Field(
        description="Total number of Linear tickets created in this run.",
    )
    skipped_duplicates: int = Field(
        description="Total findings skipped because they matched existing open tickets.",
    )
    dry_run: bool = Field(
        description="Whether this was a dry run (no tickets actually created).",
    )
    summary: str = Field(
        default="",
        description="Human-readable sweep summary table.",
    )
