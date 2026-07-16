# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccAutoauthorWindow — the N=10 observation counter (OMN-14393, report-only).

Pure COMPUTE. Aggregates the durable OCC auto-authoring observation trail into a
trailing consecutive-clean streak and reports whether it has reached N. This is
the evidence gate for the future fail-closed flip (design §4): it counts, it does
NOT flip anything, retire anything, or block anything.

A "clean" observation is one that is machine-minted AND byte-reproducible from
``compute_companion_plan`` AND whose product PR passed occ-preflight
(``ModelOccAutoauthorObservation.is_clean``). The streak is measured from the END
of the chronologically-sorted trail, so a single non-clean observation resets it —
exactly the "N consecutive clean" acceptance criterion.
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_result import (
    ModelOccAutoauthorWindowResult,
)

logger = logging.getLogger(__name__)


def aggregate_autoauthor_window(
    request: ModelOccAutoauthorWindowRequest,
) -> ModelOccAutoauthorWindowResult:
    """Count the trailing consecutive-clean streak over the observation trail. PURE.

    Observations are sorted deterministically by ``(observed_at, product_repo,
    product_pr_number)`` so the counter is order-independent. The streak is the
    length of the trailing run of ``is_clean`` observations; any non-clean
    observation resets it to zero. ``flip_ready`` is a pure comparison against N
    and carries NO side effect — the operator reads it to decide the future flip.
    """
    ordered = sorted(
        request.observations,
        key=lambda o: (o.observed_at, o.product_repo, o.product_pr_number),
    )

    streak = 0
    streak_broken_by = ""
    for obs in ordered:
        if obs.is_clean:
            streak += 1
        else:
            streak = 0
            streak_broken_by = f"{obs.product_repo}#{obs.product_pr_number}"

    flip_ready = streak >= request.required_streak
    total = len(ordered)
    summary = (
        f"{streak}/{request.required_streak} consecutive clean machine-minted "
        f"companions over {total} observation(s) — "
        f"{'FLIP-READY (evidence met, operator-gated)' if flip_ready else 'not yet flip-ready'}"
    )

    return ModelOccAutoauthorWindowResult(
        consecutive_clean=streak,
        required_streak=request.required_streak,
        flip_ready=flip_ready,
        total_observations=total,
        streak_broken_by=streak_broken_by,
        summary=summary,
    )


class HandlerOccAutoauthorWindow:
    """Pure COMPUTE handler: aggregate the auto-authoring observation window."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        request: ModelOccAutoauthorWindowRequest,
    ) -> ModelOccAutoauthorWindowResult:
        result = aggregate_autoauthor_window(request)
        logger.info(
            "occ_autoauthor_window: %d/%d consecutive clean over %d observations (flip_ready=%s)",
            result.consecutive_clean,
            result.required_streak,
            result.total_observations,
            result.flip_ready,
        )
        return result


__all__ = [
    "HandlerOccAutoauthorWindow",
    "aggregate_autoauthor_window",
]
