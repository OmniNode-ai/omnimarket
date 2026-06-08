# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextRoi — pure N-arm factor-matrix scorer (OMN-12797 P2-2/P2-3).

COMPUTE node: pure, idempotent. Accepts pre-captured (task x arm x trial) rows
(fixture/replay mode) or caller-supplied live rows and produces per-cell
aggregate metrics plus a cross-arm summary with deltas vs the 'off' baseline.

Key invariants (enforced here, not just declared in models):
  - Missing required factor in a row -> fail that row immediately
      (failure_stage=missing_required_factor), never warn silently.
  - Missing optional factor -> warn (never silent-green).
  - full_guidance_negative_control arm budget failures are scored separately
    from generation failures and never counted as generation evidence.
  - full_guidance_negative_control arm is NEVER ranked as preferred arm.
  - Runtime-observed mode is gated on coordinated deploy (plan §Parallelization);
    fixture_mode=False emits a clear failure, not empty output.

Proof classification mirrors node_on_vs_off_experiment_compute:
  - REPLAY_PROVEN: fixture_mode=True, all rows pre-captured.
  - RUNTIME_OBSERVED_ONLY: fixture_mode=False (live runner rows).
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_request import (
    ModelArmRunRow,
    ModelContextRoiRequest,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_result import (
    EnumProofClass,
    ModelArmAggregateRow,
    ModelArmSummaryRow,
    ModelContextRoiResult,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
    ModelFactorArm,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_matrix import (
    build_canonical_factor_matrix,
)
from omnimarket.nodes.node_context_roi_compute.models.model_task_manifest import (
    EnumFailureStage,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _arm_by_label(
    matrix: tuple[ModelFactorArm, ...], label: EnumArmLabel
) -> ModelFactorArm | None:
    for arm in matrix:
        if arm.label == label:
            return arm
    return None


def _validate_row_required_factors(
    row: ModelArmRunRow,
    arm: ModelFactorArm,
) -> list[str]:
    """Return errors if required factors are absent from the row's factors_present."""
    present = set(row.factors_present)
    missing = [f for f in arm.required_factors if f not in present]
    if missing:
        return [
            f"arm {arm.label}: task {row.task_id} trial {row.trial_index}: "
            f"missing required factors: "
            f"{', '.join(f.value for f in missing)}"
        ]
    return []


def _warn_optional_factors(
    row: ModelArmRunRow,
    arm: ModelFactorArm,
) -> list[str]:
    """Return warnings for optional factors absent from factors_present.

    Optional-factor absence is never silent-green; it must be warned.
    factors_warned_absent on the row is the authoritative record from the runner;
    we cross-check it here against the arm's optional_factors declaration.
    """
    present = set(row.factors_present)
    arm_optional = set(arm.optional_factors)
    absent_optional = arm_optional - present
    # Warn for any optional factor not in factors_warned_absent
    already_warned = set(row.factors_warned_absent)
    unwarned = absent_optional - already_warned
    warnings: list[str] = []
    # Emit warnings for all absent optional factors (whether runner warned or not)
    for factor in sorted(absent_optional, key=lambda f: f.value):
        prefix = "" if factor in already_warned else "[scorer-detected] "
        warnings.append(
            f"{prefix}arm {arm.label}: task {row.task_id} trial {row.trial_index}: "
            f"optional factor absent: {factor.value}"
        )
    if unwarned:
        warnings.append(
            f"arm {arm.label}: task {row.task_id} trial {row.trial_index}: "
            f"scorer detected absent optional factors not warned by runner: "
            f"{', '.join(f.value for f in sorted(unwarned, key=lambda x: x.value))}"
        )
    return warnings


def _safe_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _safe_mean_numeric(values: list[int]) -> float | None:
    """Mean over a list of integers; returns float or None when empty."""
    if not values:
        return None
    return sum(values) / len(values)


def _build_aggregate_row(
    task_id: str,
    arm_label: EnumArmLabel,
    rows: list[ModelArmRunRow],
    row_warnings: list[str],
    missing_required_count: int,
) -> ModelArmAggregateRow:
    trial_count = len(rows)
    first_pass_count = sum(1 for r in rows if r.first_pass_success)
    final_success_count = sum(1 for r in rows if r.final_success)
    budget_fail_count = sum(
        1 for r in rows if r.failure_stage == EnumFailureStage.BUDGET_FAIL
    )
    gen_fail_count = sum(
        1 for r in rows if r.failure_stage == EnumFailureStage.GENERATION
    )

    # attempt_count: mean over all rows; first-pass rate remains the headline signal.
    attempt_values = [r.attempt_count for r in rows]
    mean_attempts = _safe_mean_numeric(attempt_values)

    # token/cost: only from rows where captured; cast int to float for _safe_mean
    prompt_values: list[float] = [
        float(r.prompt_tokens) for r in rows if r.prompt_tokens is not None
    ]
    completion_values: list[float] = [
        float(r.completion_tokens) for r in rows if r.completion_tokens is not None
    ]
    cost_values: list[float] = [
        r.estimated_cost_usd for r in rows if r.estimated_cost_usd is not None
    ]

    first_pass_rate = first_pass_count / trial_count if trial_count > 0 else 0.0
    final_success_rate = final_success_count / trial_count if trial_count > 0 else 0.0

    mean_prompt = _safe_mean(prompt_values)
    mean_completion = _safe_mean(completion_values)
    mean_cost = _safe_mean(cost_values)

    return ModelArmAggregateRow(
        task_id=task_id,
        arm_label=arm_label,
        trial_count=trial_count,
        first_pass_success_count=first_pass_count,
        final_success_count=final_success_count,
        first_pass_rate=round(first_pass_rate, 6),
        final_success_rate=round(final_success_rate, 6),
        mean_attempt_count=round(mean_attempts, 4)
        if mean_attempts is not None
        else None,
        budget_fail_count=budget_fail_count,
        generation_fail_count=gen_fail_count,
        missing_required_factor_count=missing_required_count,
        mean_prompt_tokens=round(mean_prompt, 2) if mean_prompt is not None else None,
        mean_completion_tokens=round(mean_completion, 2)
        if mean_completion is not None
        else None,
        mean_cost_usd=round(mean_cost, 8) if mean_cost is not None else None,
        warnings=tuple(row_warnings),
    )


def _build_arm_summary(
    arm: ModelFactorArm,
    cell_rows: list[ModelArmAggregateRow],
    off_summary: ModelArmSummaryRow | None,
) -> ModelArmSummaryRow:
    task_count = len(cell_rows)

    mean_first_pass = (
        sum(r.first_pass_rate for r in cell_rows) / task_count if task_count else 0.0
    )
    mean_final_success = (
        sum(r.final_success_rate for r in cell_rows) / task_count if task_count else 0.0
    )

    cost_values = [r.mean_cost_usd for r in cell_rows if r.mean_cost_usd is not None]
    mean_cost = _safe_mean(cost_values)

    total_budget_fails = sum(r.budget_fail_count for r in cell_rows)
    total_missing_required = sum(r.missing_required_factor_count for r in cell_rows)
    total_optional_warnings = sum(len(r.warnings) for r in cell_rows)

    # Deltas vs off baseline
    first_pass_delta: float | None = None
    final_success_delta: float | None = None
    cost_delta: float | None = None
    if off_summary is not None:
        first_pass_delta = round(mean_first_pass - off_summary.mean_first_pass_rate, 6)
        final_success_delta = round(
            mean_final_success - off_summary.mean_final_success_rate, 6
        )
        if mean_cost is not None and off_summary.mean_cost_usd is not None:
            cost_delta = round(mean_cost - off_summary.mean_cost_usd, 8)

    # Cost per success
    cost_per_success: float | None = None
    if mean_cost is not None and mean_final_success > 0.0:
        cost_per_success = round(mean_cost / mean_final_success, 8)

    return ModelArmSummaryRow(
        arm_label=arm.label,
        is_negative_control=arm.is_negative_control,
        task_count=task_count,
        mean_first_pass_rate=round(mean_first_pass, 6),
        mean_final_success_rate=round(mean_final_success, 6),
        first_pass_rate_delta_vs_off=first_pass_delta,
        final_success_rate_delta_vs_off=final_success_delta,
        mean_cost_usd=round(mean_cost, 8) if mean_cost is not None else None,
        cost_delta_vs_off_usd=cost_delta,
        cost_per_success_usd=cost_per_success,
        total_budget_fail_count=total_budget_fails,
        total_missing_required_factor_count=total_missing_required,
        optional_factor_warning_count=total_optional_warnings,
    )


def _select_preferred_arm(
    summaries: tuple[ModelArmSummaryRow, ...],
) -> EnumArmLabel | None:
    """Return the arm with the highest first_pass_rate_delta_vs_off.

    full_guidance_negative_control is explicitly excluded.
    Returns None when no eligible arm has a positive delta or when
    the off baseline is absent (all deltas are None).
    """
    best_delta: float | None = None
    best_label: EnumArmLabel | None = None
    for summary in summaries:
        if summary.is_negative_control:
            continue
        if summary.arm_label == EnumArmLabel.OFF:
            continue
        delta = summary.first_pass_rate_delta_vs_off
        if delta is None:
            continue
        if best_delta is None or delta > best_delta:
            best_delta = delta
            best_label = summary.arm_label
    # Only declare a preferred arm if it beats baseline
    if best_delta is not None and best_delta > 0.0:
        return best_label
    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerContextRoi:
    """COMPUTE — pure N-arm factor-matrix scorer for the context-ROI experiment.

    fixture_mode=True (default): all rows are pre-supplied constants.
        Produces REPLAY_PROVEN bundles with no I/O.

    fixture_mode=False: reserved for live runner rows (runtime-observed);
        gated on coordinated deploy per plan §Parallelization. Emits a clear
        failure rather than silently producing empty output.
    """

    def handle(self, request: ModelContextRoiRequest) -> ModelContextRoiResult:
        if not request.fixture_mode:
            return ModelContextRoiResult(
                status="failed",
                run_id=request.run_id,
                manifest_id=request.manifest_id,
                failure_class="runtime_mode_not_implemented",
                errors=(
                    "runtime-observed mode requires live runner integration "
                    "(gated on coordinated deploy per plan §Parallelization); "
                    "use fixture_mode=True for replay-proven scoring",
                ),
            )

        return self._score(request)

    def _score(self, request: ModelContextRoiRequest) -> ModelContextRoiResult:
        matrix = build_canonical_factor_matrix()
        arm_map: dict[EnumArmLabel, ModelFactorArm] = {arm.label: arm for arm in matrix}

        all_errors: list[str] = []
        all_warnings: list[str] = []

        # Group rows by (task_id, arm_label)
        cell_rows: dict[tuple[str, EnumArmLabel], list[ModelArmRunRow]] = defaultdict(
            list
        )
        cell_warnings: dict[tuple[str, EnumArmLabel], list[str]] = defaultdict(list)
        cell_missing_required: dict[tuple[str, EnumArmLabel], int] = defaultdict(int)

        for row in request.rows:
            arm = arm_map.get(row.arm_label)
            if arm is None:
                all_errors.append(
                    f"unknown arm_label {row.arm_label!r} in row "
                    f"(task={row.task_id}, trial={row.trial_index})"
                )
                continue

            # Enforce required factors: fail the row, not a warning
            factor_errors = _validate_row_required_factors(row, arm)
            if factor_errors:
                all_errors.extend(factor_errors)
                cell_missing_required[(row.task_id, row.arm_label)] += 1
                # Still include the row in the cell with its failure_stage
                # (recorded as missing_required_factor in the aggregate counts)

            # Warn on absent optional factors (never silent-green)
            opt_warnings = _warn_optional_factors(row, arm)
            cell_warnings[(row.task_id, row.arm_label)].extend(opt_warnings)
            all_warnings.extend(opt_warnings)

            cell_rows[(row.task_id, row.arm_label)].append(row)

        if all_errors:
            return ModelContextRoiResult(
                status="failed",
                run_id=request.run_id,
                manifest_id=request.manifest_id,
                failure_class="required_factor_missing",
                errors=tuple(all_errors),
                warnings=tuple(all_warnings),
            )

        # Build per-cell aggregate rows
        aggregate_rows: list[ModelArmAggregateRow] = []
        for (task_id, arm_label), rows_in_cell in cell_rows.items():
            agg = _build_aggregate_row(
                task_id=task_id,
                arm_label=arm_label,
                rows=rows_in_cell,
                row_warnings=cell_warnings[(task_id, arm_label)],
                missing_required_count=cell_missing_required[(task_id, arm_label)],
            )
            aggregate_rows.append(agg)

        # Sort deterministically: by arm label canonical order, then task_id
        arm_order = {arm.label: i for i, arm in enumerate(matrix)}
        aggregate_rows.sort(key=lambda r: (arm_order.get(r.arm_label, 999), r.task_id))

        # Build off-baseline summary first (needed for deltas)
        off_cells = [r for r in aggregate_rows if r.arm_label == EnumArmLabel.OFF]
        off_summary: ModelArmSummaryRow | None = None
        if off_cells:
            off_arm = arm_map[EnumArmLabel.OFF]
            off_summary = _build_arm_summary(off_arm, off_cells, off_summary=None)

        # Build per-arm summaries in canonical order
        summaries: list[ModelArmSummaryRow] = []
        for arm in matrix:
            cells_for_arm = [r for r in aggregate_rows if r.arm_label == arm.label]
            if not cells_for_arm:
                continue
            ref = off_summary if arm.label != EnumArmLabel.OFF else None
            summary = _build_arm_summary(arm, cells_for_arm, ref)
            summaries.append(summary)

        preferred = _select_preferred_arm(tuple(summaries))

        return ModelContextRoiResult(
            status="ok",
            run_id=request.run_id,
            manifest_id=request.manifest_id,
            arm_rows=tuple(aggregate_rows),
            arm_summary=tuple(summaries),
            preferred_arm=preferred,
            proof_class=EnumProofClass.REPLAY_PROVEN,
            warnings=tuple(all_warnings),
        )


__all__ = ["HandlerContextRoi"]
