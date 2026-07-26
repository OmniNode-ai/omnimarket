# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for the report anchor-probe EFFECT (OMN-15164).

This is the SEAM surface: OMN-15163's report-validation COMPUTE node consumes
this model as (part of) its own input. See that ticket's contract/PR body for
the field-by-field match; do not rename fields here without updating that
lane's contract in the same PR.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_report_anchor_probe_effect.models.model_probe_outcome import (
    ModelPathProbeResult,
    ModelPrProbeResult,
    ModelShaProbeResult,
)


class ModelReportAnchorProbeResult(BaseModel):
    """Typed probe results for every claim in the request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    sha_results: tuple[ModelShaProbeResult, ...] = Field(default=())
    path_results: tuple[ModelPathProbeResult, ...] = Field(default=())
    pr_result: ModelPrProbeResult | None = Field(default=None)

    @property
    def all_resolved(self) -> bool:
        """True iff every probed claim (sha, path, and PR if present) resolved.

        An empty request (no claims at all) is vacuously ``True`` -- absence of
        a claim is not a failed claim; the caller decides whether a report
        with zero content-anchor fields is itself acceptable.
        """
        if self.pr_result is not None and not self.pr_result.resolved:
            return False
        return all(r.resolved for r in self.sha_results) and all(
            r.resolved for r in self.path_results
        )

    @property
    def total_claims(self) -> int:
        return (
            len(self.sha_results)
            + len(self.path_results)
            + (1 if self.pr_result is not None else 0)
        )


__all__ = ["ModelReportAnchorProbeResult"]
