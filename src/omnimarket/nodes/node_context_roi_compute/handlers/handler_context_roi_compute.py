# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextRoiCompute — N-arm context ROI COMPUTE scorer (OMN-12796).

Pure, deterministic. Accepts frozen run rows (one per task x factor_subset x trial),
groups them by factor_subset, aggregates per-subset statistics, then computes deltas
vs the designated off arm.

HEADLINE metrics per plan §P2-5:
  first_pass_rate + cost_per_success (NOT mean_attempts at max_attempts=2).

Proof classification:
  REPLAY_PROVEN: fixture_mode=True; all rows carry pre-captured token counts.
  RUNTIME_OBSERVED_ONLY: fixture_mode=False; rows from live inference (not implemented;
    gated on coordinated lane deploy per OMN-12796).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_request import (
    ModelContextRoiPricing,
    ModelContextRoiRequest,
    ModelContextRoiRow,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_result import (
    EnumProofClass,
    ModelContextRoiResult,
    ModelContextRoiSubsetSummary,
)


def _row_cost(row: ModelContextRoiRow, pricing: ModelContextRoiPricing) -> float:
    """Derive per-row cost: use pre-computed value if present, else price tokens."""
    if row.estimated_cost_usd is not None:
        return row.estimated_cost_usd
    return round(
        (
            row.prompt_tokens * pricing.prompt_cost_per_1k
            + row.completion_tokens * pricing.completion_cost_per_1k
        )
        / 1000.0,
        12,
    )


def _variance(values: Sequence[int | float]) -> float:
    """Population variance; 0.0 for a single-element list."""
    if len(values) <= 1:
        return 0.0
    mean = float(statistics.mean(values))
    return float(sum((v - mean) ** 2 for v in values)) / len(values)


def _aggregate_subset(
    label: str,
    rows: list[ModelContextRoiRow],
    pricing: ModelContextRoiPricing,
) -> _SubsetStats:
    """Compute all per-subset statistics from a list of rows."""
    attempt_counts = [r.attempt_count for r in rows]
    first_pass_successes = [r.first_pass_success for r in rows]
    final_successes = [r.final_success for r in rows]
    prompt_tokens = [r.prompt_tokens for r in rows]
    completion_tokens = [r.completion_tokens for r in rows]
    costs = [_row_cost(r, pricing) for r in rows]

    n = len(rows)
    total_cost = round(sum(costs), 12)
    success_count = sum(1 for s in final_successes if s)
    cost_per_success = (total_cost / success_count) if success_count > 0 else None

    return _SubsetStats(
        factor_subset=label,
        row_count=n,
        mean_attempts=statistics.mean(attempt_counts),
        median_attempts=float(statistics.median(attempt_counts)),
        attempt_count_variance=_variance(attempt_counts),
        first_pass_rate=sum(1 for s in first_pass_successes if s) / n,
        final_pass_rate=success_count / n,
        mean_prompt_tokens=statistics.mean(prompt_tokens),
        mean_completion_tokens=statistics.mean(completion_tokens),
        total_cost_usd=total_cost,
        cost_per_success_usd=cost_per_success,
    )


class _SubsetStats:
    """Intermediate holder for per-subset aggregates before delta computation."""

    __slots__ = (
        "attempt_count_variance",
        "cost_per_success_usd",
        "factor_subset",
        "final_pass_rate",
        "first_pass_rate",
        "mean_attempts",
        "mean_completion_tokens",
        "mean_prompt_tokens",
        "median_attempts",
        "row_count",
        "total_cost_usd",
    )

    def __init__(
        self,
        factor_subset: str,
        row_count: int,
        mean_attempts: float,
        median_attempts: float,
        attempt_count_variance: float,
        first_pass_rate: float,
        final_pass_rate: float,
        mean_prompt_tokens: float,
        mean_completion_tokens: float,
        total_cost_usd: float,
        cost_per_success_usd: float | None,
    ) -> None:
        self.factor_subset = factor_subset
        self.row_count = row_count
        self.mean_attempts = mean_attempts
        self.median_attempts = median_attempts
        self.attempt_count_variance = attempt_count_variance
        self.first_pass_rate = first_pass_rate
        self.final_pass_rate = final_pass_rate
        self.mean_prompt_tokens = mean_prompt_tokens
        self.mean_completion_tokens = mean_completion_tokens
        self.total_cost_usd = total_cost_usd
        self.cost_per_success_usd = cost_per_success_usd


def _build_summary(
    stats: _SubsetStats,
    off: _SubsetStats,
) -> ModelContextRoiSubsetSummary:
    """Produce the final summary model for one subset, deltas computed vs off."""
    cps_delta: float | None = None
    if stats.cost_per_success_usd is not None and off.cost_per_success_usd is not None:
        cps_delta = round(stats.cost_per_success_usd - off.cost_per_success_usd, 12)

    return ModelContextRoiSubsetSummary(
        factor_subset=stats.factor_subset,
        row_count=stats.row_count,
        mean_attempts=stats.mean_attempts,
        median_attempts=stats.median_attempts,
        attempt_count_variance=stats.attempt_count_variance,
        first_pass_rate=stats.first_pass_rate,
        final_pass_rate=stats.final_pass_rate,
        mean_prompt_tokens=stats.mean_prompt_tokens,
        mean_completion_tokens=stats.mean_completion_tokens,
        total_cost_usd=stats.total_cost_usd,
        cost_per_success_usd=stats.cost_per_success_usd,
        first_pass_rate_delta_vs_off=round(
            stats.first_pass_rate - off.first_pass_rate, 12
        ),
        final_pass_rate_delta_vs_off=round(
            stats.final_pass_rate - off.final_pass_rate, 12
        ),
        mean_prompt_token_delta_vs_off=round(
            stats.mean_prompt_tokens - off.mean_prompt_tokens, 12
        ),
        mean_completion_token_delta_vs_off=round(
            stats.mean_completion_tokens - off.mean_completion_tokens, 12
        ),
        cost_per_success_delta_vs_off=cps_delta,
    )


class HandlerContextRoiCompute:
    """COMPUTE — pure N-arm context ROI scorer.

    fixture_mode=True (default): all rows carry pre-captured token counts.
    Produces REPLAY_PROVEN bundles with no I/O.

    fixture_mode=False: reserved for runtime-observed mode; gated on the
    coordinated lane deploy (OMN-12796 / plan §P2-5 deploy sequencing).
    Emits a clear failure rather than silently producing empty output.
    """

    def handle(self, request: ModelContextRoiRequest) -> ModelContextRoiResult:
        generated_at = datetime.now(tz=UTC).isoformat()

        if not request.fixture_mode:
            return ModelContextRoiResult(
                status="failed",
                run_id=request.run_id,
                model_id=request.model_id,
                proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                failure_class="runtime_mode_not_implemented",
                errors=(
                    "runtime-observed mode requires live inference integration "
                    "(gated on coordinated lane deploy per OMN-12796 plan §P2-5); "
                    "use fixture_mode=True for replay-proven evidence",
                ),
                generated_at=generated_at,
            )

        return self._handle_fixture(request, generated_at)

    def _handle_fixture(
        self,
        request: ModelContextRoiRequest,
        generated_at: str,
    ) -> ModelContextRoiResult:
        # Group rows by factor_subset
        by_subset: dict[str, list[ModelContextRoiRow]] = defaultdict(list)
        for row in request.rows:
            by_subset[row.factor_subset].append(row)

        # Require the off arm to be present
        if request.off_arm_label not in by_subset:
            return ModelContextRoiResult(
                status="failed",
                run_id=request.run_id,
                model_id=request.model_id,
                proof_class=EnumProofClass.REPLAY_PROVEN,
                failure_class="missing_off_arm",
                errors=(
                    f"no rows found for off arm '{request.off_arm_label}'; "
                    "cannot compute deltas without a baseline",
                ),
                generated_at=generated_at,
            )

        # Aggregate each subset
        all_stats: dict[str, _SubsetStats] = {}
        for label, rows in by_subset.items():
            all_stats[label] = _aggregate_subset(label, rows, request.pricing)

        off_stats = all_stats[request.off_arm_label]

        # Build sorted summaries (off arm first, then remaining alphabetically)
        ordered_labels = [
            request.off_arm_label,
            *sorted(lbl for lbl in all_stats if lbl != request.off_arm_label),
        ]
        summaries = tuple(
            _build_summary(all_stats[lbl], off_stats) for lbl in ordered_labels
        )

        return ModelContextRoiResult(
            status="ok",
            run_id=request.run_id,
            model_id=request.model_id,
            proof_class=EnumProofClass.REPLAY_PROVEN,
            subset_summaries=summaries,
            generated_at=generated_at,
        )


__all__ = ["HandlerContextRoiCompute"]
