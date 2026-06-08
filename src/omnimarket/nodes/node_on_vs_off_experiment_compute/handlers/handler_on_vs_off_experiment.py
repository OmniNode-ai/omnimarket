# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOnVsOffExperiment — pure deterministic ON-vs-OFF cost evidence harness (OMN-12661).

COMPUTE node: pure, idempotent. Accepts a fixed task set with pre-captured token
counts (fixture/replay mode) or caller-supplied counts and produces a cost-delta
evidence bundle with an explicit proof classification.

Bounded minimum per OMN-12661:
  fixed task set -> ON path + OFF path -> token counts -> cost totals -> summary report

Proof classification:
  - REPLAY_PROVEN: all tasks carry pre-captured token counts (fixture_mode=True);
    harness is fully offline and deterministic.
  - RUNTIME_OBSERVED_ONLY: token counts from live inference (fixture_mode=False);
    results depend on live model state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_on_vs_off_experiment_compute.models.model_on_vs_off_request import (
    ModelOnVsOffRequest,
    ModelOnVsOffTask,
)
from omnimarket.nodes.node_on_vs_off_experiment_compute.models.model_on_vs_off_result import (
    EnumProofClass,
    ModelOnVsOffCostRow,
    ModelOnVsOffResult,
    ModelOnVsOffSummaryReport,
)


def _compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    prompt_cost_per_1k: float,
    completion_cost_per_1k: float,
) -> float:
    return round(
        (
            prompt_tokens * prompt_cost_per_1k
            + completion_tokens * completion_cost_per_1k
        )
        / 1000.0,
        12,
    )


def _validate_fixture_task(task: ModelOnVsOffTask) -> list[str]:
    """Validate that all required token counts are present for fixture mode."""
    errors: list[str] = []
    if task.on_prompt_tokens is None:
        errors.append(
            f"task {task.task_id}: on_prompt_tokens is required in fixture mode"
        )
    if task.on_completion_tokens is None:
        errors.append(
            f"task {task.task_id}: on_completion_tokens is required in fixture mode"
        )
    if task.off_prompt_tokens is None:
        errors.append(
            f"task {task.task_id}: off_prompt_tokens is required in fixture mode"
        )
    if task.off_completion_tokens is None:
        errors.append(
            f"task {task.task_id}: off_completion_tokens is required in fixture mode"
        )
    return errors


def _build_row(
    task: ModelOnVsOffTask,
    on_pt: int,
    on_ct: int,
    off_pt: int,
    off_ct: int,
    prompt_cost_per_1k: float,
    completion_cost_per_1k: float,
) -> ModelOnVsOffCostRow:
    on_cost = _compute_cost(on_pt, on_ct, prompt_cost_per_1k, completion_cost_per_1k)
    off_cost = _compute_cost(off_pt, off_ct, prompt_cost_per_1k, completion_cost_per_1k)
    on_total = on_pt + on_ct
    off_total = off_pt + off_ct
    return ModelOnVsOffCostRow(
        task_id=task.task_id,
        on_prompt_tokens=on_pt,
        on_completion_tokens=on_ct,
        on_total_tokens=on_total,
        on_cost_usd=on_cost,
        off_prompt_tokens=off_pt,
        off_completion_tokens=off_ct,
        off_total_tokens=off_total,
        off_cost_usd=off_cost,
        cost_delta_usd=round(on_cost - off_cost, 12),
        token_delta=on_total - off_total,
    )


def _build_summary(
    run_id: str,
    model_id: str,
    rows: tuple[ModelOnVsOffCostRow, ...],
    proof_class: EnumProofClass,
    generated_at: str,
) -> ModelOnVsOffSummaryReport:
    total_on_cost = round(sum(r.on_cost_usd for r in rows), 12)
    total_off_cost = round(sum(r.off_cost_usd for r in rows), 12)
    total_on_tokens = sum(r.on_total_tokens for r in rows)
    total_off_tokens = sum(r.off_total_tokens for r in rows)
    cost_delta = round(total_on_cost - total_off_cost, 12)
    cost_delta_pct = (
        round((cost_delta / total_off_cost) * 100.0, 6)
        if total_off_cost != 0.0
        else 0.0
    )
    return ModelOnVsOffSummaryReport(
        run_id=run_id,
        model_id=model_id,
        task_count=len(rows),
        total_on_cost_usd=total_on_cost,
        total_off_cost_usd=total_off_cost,
        total_cost_delta_usd=cost_delta,
        total_on_tokens=total_on_tokens,
        total_off_tokens=total_off_tokens,
        total_token_delta=total_on_tokens - total_off_tokens,
        cost_delta_pct=cost_delta_pct,
        proof_class=proof_class,
        generated_at=generated_at,
    )


class HandlerOnVsOffExperiment:
    """COMPUTE — pure ON-vs-OFF cost evidence harness.

    fixture_mode=True (default): all token counts are caller-supplied via
    ModelOnVsOffTask fields. Produces REPLAY_PROVEN bundles with no I/O.

    fixture_mode=False: reserved for runtime-observed mode; requires live
    inference integration (not implemented in this PR — gated on redeploy,
    see OMN-12743 part (d)).
    """

    def handle(self, request: ModelOnVsOffRequest) -> ModelOnVsOffResult:
        generated_at = datetime.now(tz=UTC).isoformat()

        if request.fixture_mode:
            return self._handle_fixture(request, generated_at)
        # Runtime-observed mode: gated on .201 redeploy (OMN-12743 part (d)).
        # Emit a clear failure rather than silently producing empty output.
        return ModelOnVsOffResult(
            status="failed",
            failure_class="runtime_mode_not_implemented",
            errors=(
                "runtime-observed mode requires live LLM inference integration "
                "(gated on .201 redeploy per OMN-12743 part (d)); "
                "use fixture_mode=True for replay-proven evidence",
            ),
        )

    def _handle_fixture(
        self,
        request: ModelOnVsOffRequest,
        generated_at: str,
    ) -> ModelOnVsOffResult:
        # Validate all tasks have required token counts
        all_errors: list[str] = []
        for task in request.tasks:
            all_errors.extend(_validate_fixture_task(task))
        if all_errors:
            return ModelOnVsOffResult(
                status="failed",
                failure_class="missing_fixture_token_counts",
                errors=tuple(all_errors),
            )

        rows: list[ModelOnVsOffCostRow] = []
        for task in request.tasks:
            # All token counts validated non-None above
            row = _build_row(
                task=task,
                on_pt=task.on_prompt_tokens,  # type: ignore[arg-type]
                on_ct=task.on_completion_tokens,  # type: ignore[arg-type]
                off_pt=task.off_prompt_tokens,  # type: ignore[arg-type]
                off_ct=task.off_completion_tokens,  # type: ignore[arg-type]
                prompt_cost_per_1k=request.pricing.prompt_cost_per_1k,
                completion_cost_per_1k=request.pricing.completion_cost_per_1k,
            )
            rows.append(row)

        rows_tuple = tuple(rows)
        summary = _build_summary(
            run_id=request.run_id,
            model_id=request.model_id,
            rows=rows_tuple,
            proof_class=EnumProofClass.REPLAY_PROVEN,
            generated_at=generated_at,
        )
        return ModelOnVsOffResult(
            status="ok",
            rows=rows_tuple,
            summary=summary,
        )


__all__ = ["HandlerOnVsOffExperiment"]
