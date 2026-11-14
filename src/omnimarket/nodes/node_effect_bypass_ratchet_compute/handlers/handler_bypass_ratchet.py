# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBypassRatchet: does one repository's bypass count stay at or under its baseline (OMN-24432).

Definition-B compute: ``handle(request: ModelBypassRatchetRequest) -> ModelBypassRatchetVerdict``. No clock, no I/O.
A count that rose, a repository the baseline does not list, and a baseline that grew past the merge base's copy refuse.
A count that fell is noted as lowerable and passes: a change that removes a bypass lowers the baseline in the same change.
"""

from __future__ import annotations

from omnimarket.nodes.node_effect_bypass_ratchet_compute.models import (
    EnumBypassCategory,
    EnumBypassFinding,
    ModelBypassCategoryReading,
    ModelBypassRatchetRequest,
    ModelBypassRatchetVerdict,
)


class HandlerBypassRatchet:
    """Judge one repository's scan against its baseline."""

    def handle(self, request: ModelBypassRatchetRequest) -> ModelBypassRatchetVerdict:
        inventory = request.inventory
        repo = inventory.repository
        total_now = inventory.counts.total
        base_row = request.baseline.repositories.get(repo)
        if base_row is None:
            return ModelBypassRatchetVerdict(
                repository=repo,
                ok=False,
                total_now=total_now,
                total_baseline=0,
                categories=(),
                findings=(EnumBypassFinding.REPOSITORY_NOT_IN_BASELINE,),
            )
        readings = tuple(
            ModelBypassCategoryReading(
                category=category,
                now=inventory.counts.get(category),
                baseline=base_row.get(category),
                rose=inventory.counts.get(category) > base_row.get(category),
            )
            for category in EnumBypassCategory
        )
        rose = any(r.rose for r in readings)
        fell = any(r.now < r.baseline for r in readings)
        findings: list[EnumBypassFinding] = []
        if rose:
            findings.append(EnumBypassFinding.CATEGORY_ROSE)
        if self._baseline_grew(request, repo):
            findings.append(EnumBypassFinding.BASELINE_GREW_VS_BASE)
        if fell and not rose:
            findings.append(EnumBypassFinding.BASELINE_LOWERABLE)
        refusing = {
            EnumBypassFinding.CATEGORY_ROSE,
            EnumBypassFinding.BASELINE_GREW_VS_BASE,
        }
        return ModelBypassRatchetVerdict(
            repository=repo,
            ok=not any(f in refusing for f in findings),
            total_now=total_now,
            total_baseline=base_row.total,
            categories=readings,
            findings=tuple(findings),
        )

    @staticmethod
    def _baseline_grew(request: ModelBypassRatchetRequest, repo: str) -> bool:
        """A baseline category above the merge base's, or a new repository row that carries any count."""
        if request.base_baseline is None:
            return False
        now_row = request.baseline.repositories[repo]
        before = request.base_baseline.repositories.get(repo)
        if before is None:
            return now_row.total > 0
        return any(now_row.get(c) > before.get(c) for c in EnumBypassCategory)
