# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrHealthInput — input for the PR health monitor compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelPRInfo,
)


class ModelPrHealthInput(BaseModel):
    """Input for the PR health monitor: a list of PRs and health thresholds.

    pr_age_days is an optional map of "{repo}#{number}" -> age_in_days.
    When provided (e.g. from pr-snapshot.sh JSON output), it enables
    STALE_INACTIVE classification. When absent, STALE_INACTIVE is skipped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs: tuple[ModelPRInfo, ...] = Field(
        default_factory=tuple,
        description="PRs to classify. Populated from node_pr_snapshot_effect output.",
    )
    pr_age_days: dict[str, int] = Field(
        default_factory=dict,
        description=(
            'Map of "{repo}#{number}" -> age_in_days. '
            "Enables STALE_INACTIVE detection when provided."
        ),
    )
    stale_red_threshold_hours: int = Field(
        default=24,
        description="CI failures older than this many hours are classified STALE_RED.",
        ge=1,
    )
    stale_inactive_threshold_days: int = Field(
        default=3,
        description="PRs with no activity for this many days are classified STALE_INACTIVE.",
        ge=1,
    )


__all__: list[str] = ["ModelPrHealthInput"]
