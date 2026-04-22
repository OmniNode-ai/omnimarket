# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrHealthMonitor — classifies PRs by health status.

Pure compute node: no I/O. Consumes ModelPRInfo objects (from
node_pr_snapshot_effect) and classifies each as:

  STALE_RED       — CI failing (required_checks_pass=False), non-draft
  CONFLICTED      — mergeable == CONFLICTING
  REVIEW_BLOCKED  — CHANGES_REQUESTED, or BLOCKED merge state with failed CI
  STALE_INACTIVE  — age_days >= stale_inactive_threshold_days (when known)
  HEALTHY         — everything else

Classification is multi-label: a PR can be both STALE_RED and CONFLICTED.
The primary status is the worst (highest severity) label.

Severity order (worst first):
  STALE_RED > CONFLICTED > REVIEW_BLOCKED > STALE_INACTIVE > HEALTHY
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelPRInfo,
)
from omnimarket.nodes.node_pr_health_monitor.models.model_pr_health_input import (
    ModelPrHealthInput,
)
from omnimarket.nodes.node_pr_health_monitor.models.model_pr_health_result import (
    EnumPrHealthStatus,
    ModelPrHealthEntry,
    ModelPrHealthReport,
    ModelPrHealthSummary,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]

# Severity rank for resolving the primary status when a PR has multiple flags.
_SEVERITY: dict[EnumPrHealthStatus, int] = {
    EnumPrHealthStatus.STALE_RED: 0,
    EnumPrHealthStatus.CONFLICTED: 1,
    EnumPrHealthStatus.REVIEW_BLOCKED: 2,
    EnumPrHealthStatus.STALE_INACTIVE: 3,
    EnumPrHealthStatus.HEALTHY: 4,
}


def _classify_pr(
    pr: ModelPRInfo,
    age_days: int,
    stale_inactive_threshold_days: int,
    has_age: bool,
) -> tuple[EnumPrHealthStatus, tuple[str, ...]]:
    """Classify a single PR. Returns (primary_status, reasons)."""
    flags: list[EnumPrHealthStatus] = []
    reasons: list[str] = []

    if pr.is_draft:
        # Drafts are informational only — never flagged as unhealthy.
        return EnumPrHealthStatus.HEALTHY, ()

    # CI failing
    if not pr.required_checks_pass:
        flags.append(EnumPrHealthStatus.STALE_RED)
        reasons.append("CI failing")

    # Merge conflicts
    if pr.mergeable == "CONFLICTING":
        flags.append(EnumPrHealthStatus.CONFLICTED)
        reasons.append("merge conflict")

    # Review blocked: changes requested, or blocked merge state
    review_blocked = pr.review_decision == "CHANGES_REQUESTED" or (
        pr.merge_state_status.upper() == "BLOCKED" and not pr.required_checks_pass
    )
    if review_blocked:
        flags.append(EnumPrHealthStatus.REVIEW_BLOCKED)
        if pr.review_decision == "CHANGES_REQUESTED":
            reasons.append("changes requested")
        else:
            reasons.append("merge blocked")

    # Stale inactive (only when age is known)
    if has_age and age_days >= stale_inactive_threshold_days:
        flags.append(EnumPrHealthStatus.STALE_INACTIVE)
        reasons.append(f"no activity for {age_days}d")

    if not flags:
        return EnumPrHealthStatus.HEALTHY, ()

    primary = min(flags, key=lambda s: _SEVERITY[s])
    return primary, tuple(reasons)


class HandlerPrHealthMonitor:
    """Classify PRs by health status — pure compute, no I/O."""

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle(self, input_model: ModelPrHealthInput) -> ModelPrHealthReport:
        """Classify all PRs and return a health report.

        Args:
            input_model: PRs + optional age map + thresholds.

        Returns:
            ModelPrHealthReport with per-PR entries and aggregate summary.
        """
        logger.info("PR health monitor classifying %d PRs", len(input_model.prs))

        entries: list[ModelPrHealthEntry] = []

        for pr in input_model.prs:
            key = f"{pr.repo}#{pr.number}"
            age_days = input_model.pr_age_days.get(key, 0)
            has_age = key in input_model.pr_age_days

            status, reasons = _classify_pr(
                pr=pr,
                age_days=age_days,
                stale_inactive_threshold_days=input_model.stale_inactive_threshold_days,
                has_age=has_age,
            )
            entries.append(
                ModelPrHealthEntry(
                    pr=pr,
                    status=status,
                    reasons=reasons,
                    age_days=age_days,
                )
            )

        summary = _compute_summary(entries)
        generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        report = ModelPrHealthReport(
            entries=tuple(entries),
            summary=summary,
            generated_at=generated_at,
        )

        logger.info(
            "PR health report: total=%d healthy=%d stale_red=%d conflicted=%d "
            "review_blocked=%d stale_inactive=%d",
            summary.total,
            summary.healthy,
            summary.stale_red,
            summary.conflicted,
            summary.review_blocked,
            summary.stale_inactive,
        )
        return report


def _compute_summary(entries: list[ModelPrHealthEntry]) -> ModelPrHealthSummary:
    """Compute aggregate counts from classified entries."""
    counts: dict[EnumPrHealthStatus, int] = dict.fromkeys(EnumPrHealthStatus, 0)
    for entry in entries:
        counts[entry.status] += 1

    return ModelPrHealthSummary(
        total=len(entries),
        healthy=counts[EnumPrHealthStatus.HEALTHY],
        stale_red=counts[EnumPrHealthStatus.STALE_RED],
        stale_inactive=counts[EnumPrHealthStatus.STALE_INACTIVE],
        conflicted=counts[EnumPrHealthStatus.CONFLICTED],
        review_blocked=counts[EnumPrHealthStatus.REVIEW_BLOCKED],
    )


__all__: list[str] = ["HandlerPrHealthMonitor"]
