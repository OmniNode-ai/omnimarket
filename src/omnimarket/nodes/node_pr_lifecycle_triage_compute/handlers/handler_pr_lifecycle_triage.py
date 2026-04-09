# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLifecycleTriage — pure-compute PR triage classifier.

Takes PR inventory data and classifies each PR into a triage category:
  - GREEN: CI passing, approved, no conflicts — ready to merge
  - RED: CI failing or errored — needs fix
  - CONFLICTED: Has merge conflicts — needs rebase
  - NEEDS_REVIEW: CI ok but lacks required approval

Classification priority (first match wins):
  1. CONFLICTED — has_conflicts is True
  2. RED — ci_status is 'failing' or 'error'
  3. GREEN — ci_status is 'passing' AND approved is True
  4. NEEDS_REVIEW — all other cases (pending CI, no approval, etc.)

Zero network calls. Pure transformation on inventory data.

Related:
    - OMN-8083: Create pr_lifecycle_triage_compute Node
    - OMN-8082: pr_lifecycle_inventory_compute (upstream producer)
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_triage_category import (
    EnumPrTriageCategory,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_inventory_item import (
    ModelPrInventoryItem,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_triage_output import (
    ModelPrTriageOutput,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_triage_result import (
    ModelPrTriageResult,
)

logger = logging.getLogger(__name__)

_CI_FAILING_STATUSES: frozenset[str] = frozenset({"failing", "error", "failed"})
_CI_PASSING_STATUSES: frozenset[str] = frozenset({"passing", "success"})


def _classify_pr(pr: ModelPrInventoryItem) -> ModelPrTriageResult:
    """Classify a single PR into a triage category.

    Priority (first match wins):
    1. CONFLICTED — has merge conflicts
    2. RED — CI is failing or errored
    3. GREEN — CI passing AND approved (and no open blocking threads)
    4. NEEDS_REVIEW — everything else

    Args:
        pr: Inventory data for the PR.

    Returns:
        Triage result with category and reason.
    """
    # 1. Conflicts take top priority — can't merge regardless of CI or approval
    if pr.has_conflicts:
        return ModelPrTriageResult(
            pr_number=pr.pr_number,
            repo=pr.repo,
            category=EnumPrTriageCategory.CONFLICTED,
            reason="PR has merge conflicts and requires a rebase.",
        )

    # 2. Failing CI — needs a fix before anything else
    ci_lower = pr.ci_status.lower()
    if ci_lower in _CI_FAILING_STATUSES:
        return ModelPrTriageResult(
            pr_number=pr.pr_number,
            repo=pr.repo,
            category=EnumPrTriageCategory.RED,
            reason=f"CI status is '{pr.ci_status}' — fix required before merge.",
        )

    # 3. Green — CI passing, approved, no unresolved threads
    if ci_lower in _CI_PASSING_STATUSES and pr.approved and pr.open_threads == 0:
        return ModelPrTriageResult(
            pr_number=pr.pr_number,
            repo=pr.repo,
            category=EnumPrTriageCategory.GREEN,
            reason="CI passing, approved, and no unresolved threads — ready to merge.",
        )

    # 4. Needs review — CI ok (passing or pending/unknown) but not yet approved
    if not pr.approved:
        reason = "Awaiting approval."
        if ci_lower not in _CI_PASSING_STATUSES:
            reason = f"CI status is '{pr.ci_status}' and awaiting approval."
        elif pr.open_threads > 0:
            reason = f"Approved but has {pr.open_threads} unresolved review thread(s)."
        return ModelPrTriageResult(
            pr_number=pr.pr_number,
            repo=pr.repo,
            category=EnumPrTriageCategory.NEEDS_REVIEW,
            reason=reason,
        )

    # Approved but has open threads or non-passing CI
    if pr.open_threads > 0:
        return ModelPrTriageResult(
            pr_number=pr.pr_number,
            repo=pr.repo,
            category=EnumPrTriageCategory.NEEDS_REVIEW,
            reason=f"Approved but has {pr.open_threads} unresolved review thread(s).",
        )

    # Approved, no conflicts, no failing CI, no open threads — but CI not confirmed passing
    return ModelPrTriageResult(
        pr_number=pr.pr_number,
        repo=pr.repo,
        category=EnumPrTriageCategory.NEEDS_REVIEW,
        reason=f"CI status is '{pr.ci_status}' — waiting for CI to pass before merge.",
    )


class HandlerPrLifecycleTriage:
    """Classifies PRs from inventory data into triage categories.

    Pure compute handler — no I/O, no network calls.
    """

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        prs: tuple[ModelPrInventoryItem, ...],
    ) -> ModelPrTriageOutput:
        """Classify PRs into triage categories.

        Args:
            correlation_id: Correlation ID from the inventory event.
            prs: PR inventory items to classify.

        Returns:
            ModelPrTriageOutput with per-PR results and category counts.
        """
        logger.info(
            "Triaging %d PRs (correlation_id=%s)",
            len(prs),
            correlation_id,
        )

        results: list[ModelPrTriageResult] = []
        total_green = 0
        total_red = 0
        total_conflicted = 0
        total_needs_review = 0

        for pr in prs:
            result = _classify_pr(pr)
            results.append(result)
            if result.category == EnumPrTriageCategory.GREEN:
                total_green += 1
            elif result.category == EnumPrTriageCategory.RED:
                total_red += 1
            elif result.category == EnumPrTriageCategory.CONFLICTED:
                total_conflicted += 1
            else:
                total_needs_review += 1

        logger.info(
            "Triage complete: green=%d red=%d conflicted=%d needs_review=%d",
            total_green,
            total_red,
            total_conflicted,
            total_needs_review,
        )

        return ModelPrTriageOutput(
            correlation_id=correlation_id,
            results=tuple(results),
            total_green=total_green,
            total_red=total_red,
            total_conflicted=total_conflicted,
            total_needs_review=total_needs_review,
        )
