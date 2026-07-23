# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccAutoauthorWindow — the representative-N observation counter.

Pure COMPUTE (OMN-14393 report-only; composition-aware since OMN-14954).
Aggregates the durable OCC auto-authoring observation trail into a trailing
consecutive-clean streak and reports whether the *representative* N=10 window
criterion is met. It counts, it does NOT flip anything, retire anything, or
block anything.

Representative N (rolling-plan lane A7):

  * The unit is a distinct exact source tuple ``(product_repo,
    product_pr_number, head_sha, policy_version)`` — reruns of the same tuple
    collapse to their most recent attempt (``project_qualifying_records``).
  * Fail-reset: a non-clean representative resets the trailing streak.
  * Composition floor: the trailing clean streak must contain
    ``min_merged_path`` merged-path and ``min_runtime_gated``
    runtime/deploy-gated records. ``verification_path == unspecified``
    satisfies neither threshold (fail-closed).
  * Legacy bare-observation input reports the streak but can NEVER certify
    ``flip_ready`` — without tuple-keyed records the composition check would
    not exist, and an absent check must FAIL, not silently pass.

A "clean" observation is machine-minted AND byte-reproducible from
``compute_companion_plan`` AND occ-preflight eligible
(``ModelOccAutoauthorObservation.is_clean``).
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.events.occ_observation_record import (
    EnumOccVerificationPath,
    ModelOccObservationRecord,
    project_qualifying_records,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_result import (
    ModelOccAutoauthorWindowResult,
)

logger = logging.getLogger(__name__)


def _aggregate_records(
    request: ModelOccAutoauthorWindowRequest,
) -> ModelOccAutoauthorWindowResult:
    """Record mode: dedup to distinct tuples, fail-reset streak, composition."""
    representatives = project_qualifying_records(request.records)

    streak_records: list[ModelOccObservationRecord] = []
    streak_broken_by = ""
    for record in representatives:
        if record.observation.is_clean:
            streak_records.append(record)
        else:
            streak_records = []
            streak_broken_by = f"{record.product_repo}#{record.product_pr_number}"

    streak = len(streak_records)
    merged = sum(
        1
        for r in streak_records
        if r.verification_path is EnumOccVerificationPath.MERGED_PATH
    )
    gated = sum(
        1
        for r in streak_records
        if r.verification_path is EnumOccVerificationPath.RUNTIME_DEPLOY_GATED
    )
    composition_met = (
        merged >= request.min_merged_path and gated >= request.min_runtime_gated
    )
    flip_ready = streak >= request.required_streak and composition_met

    total = len(request.records)
    distinct = len(representatives)
    summary = (
        f"{streak}/{request.required_streak} consecutive clean distinct source "
        f"tuples ({distinct} distinct over {total} raw record(s)) — composition "
        f"{merged}/{request.min_merged_path} merged-path, "
        f"{gated}/{request.min_runtime_gated} runtime/deploy-gated "
        f"({'met' if composition_met else 'NOT met'}) — "
        f"{'FLIP-READY (evidence met, operator-gated)' if flip_ready else 'not yet flip-ready'}"
    )

    return ModelOccAutoauthorWindowResult(
        consecutive_clean=streak,
        required_streak=request.required_streak,
        flip_ready=flip_ready,
        total_observations=total,
        distinct_tuples=distinct,
        merged_path_clean=merged,
        runtime_gated_clean=gated,
        composition_met=composition_met,
        streak_broken_by=streak_broken_by,
        summary=summary,
    )


def _aggregate_legacy_observations(
    request: ModelOccAutoauthorWindowRequest,
) -> ModelOccAutoauthorWindowResult:
    """Legacy mode: streak reported, flip_ready withheld (composition unverifiable)."""
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

    total = len(ordered)
    summary = (
        f"{streak}/{request.required_streak} consecutive clean machine-minted "
        f"companions over {total} observation(s) — flip_ready withheld: "
        f"composition unverifiable from bare observations (supply tuple-keyed "
        f"records, OMN-14954)"
    )

    return ModelOccAutoauthorWindowResult(
        consecutive_clean=streak,
        required_streak=request.required_streak,
        flip_ready=False,
        total_observations=total,
        distinct_tuples=0,
        merged_path_clean=0,
        runtime_gated_clean=0,
        composition_met=False,
        streak_broken_by=streak_broken_by,
        summary=summary,
    )


def aggregate_autoauthor_window(
    request: ModelOccAutoauthorWindowRequest,
) -> ModelOccAutoauthorWindowResult:
    """Count the representative-N window over the observation trail. PURE.

    Record mode (preferred): deduplicated projection over distinct exact
    source tuples, fail-reset on non-clean representatives, composition floor
    over the trailing clean streak; ``flip_ready`` requires BOTH
    ``streak >= required_streak`` AND the composition floor.

    Legacy observations mode: order-independent streak over bare payloads;
    ``flip_ready`` is always False (fail-closed — no tuple identity, no
    composition evidence).
    """
    if request.records:
        return _aggregate_records(request)
    return _aggregate_legacy_observations(request)


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
            "occ_autoauthor_window: %d/%d consecutive clean over %d distinct tuples "
            "(%d input rows); composition merged=%d gated=%d met=%s; flip_ready=%s",
            result.consecutive_clean,
            result.required_streak,
            result.distinct_tuples,
            result.total_observations,
            result.merged_path_clean,
            result.runtime_gated_clean,
            result.composition_met,
            result.flip_ready,
        )
        return result


__all__ = [
    "HandlerOccAutoauthorWindow",
    "aggregate_autoauthor_window",
]
