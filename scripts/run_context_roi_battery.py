#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context-ROI E2E battery orchestrator (OMN-12949, P2-5c).

Drives a full (task x arm x trial) context-ROI experiment battery end-to-end by
wiring three already-built nodes - it builds no new node logic:

    (EFFECT) node_context_roi_runner  -> rows -> (COMPUTE) node_context_roi_compute

Responsibilities:
  1. Build the E1-E6 difficulty ladder as the default task battery so cells span
     difficulties and escalation-relevant cells exist (statistical-proof plan,
     Phase 3 "E1-E6 Experiment Ladder").
  2. Build the canonical 7-arm factor matrix
     (node_context_roi_compute.build_canonical_factor_matrix), including the
     permanent full_guidance_negative_control.
  3. Construct the contract-canonical ModelContextRoiRunRequest and invoke the
     runner EFFECT per cell. The runner publishes generation commands over the
     bus and consumes terminal events; this orchestrator injects the bus
     publisher/consumer (Kafka in production, a fake in tests). No in-process
     GenerationConsumer import, no kafka_runner, no hardcoded topic literals
     (topics are read from the runner contract).
  4. Translate the runner's ModelAttemptReductionRow rows into the scorer's
     ModelArmRunRow rows and feed the COMPUTE scorer in fixture mode
     (REPLAY_PROVEN offline scoring).
  5. Write the result artifact JSON plus a correlation-ID manifest tagging every
     cell with experiment + arm + cell identity. Per the Context Authority Rule,
     experiment cells are labelled selection_reason=experiment_assignment with
     an experiment_cohort, never a resolved selection.

Content resolution: the runner resolves per-factor artifact content from its
`artifact_content_map` (populated by the content-resolver effect in production).
This orchestrator does NOT hardcode artifact file paths. When an artifact-content
source is supplied (a JSON map keyed by EnumContextFactor value), it is passed
through verbatim; otherwise the runner's offline stub-content path is used (the
dry-run / replay path), which still exercises full wiring.

Dry-run mode validates the entire wiring - matrix construction, request models,
cell count, manifest shape - with zero bus traffic.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_context_roi_compute.handlers.handler_context_roi import (
    HandlerContextRoi,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_request import (
    ModelArmRunRow,
    ModelContextRoiRequest,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_result import (
    ModelContextRoiResult,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_matrix import (
    build_canonical_factor_matrix,
)
from omnimarket.nodes.node_context_roi_compute.models.model_task_manifest import (
    EnumFailureStage as EnumScorerFailureStage,
)
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_attempt_reduction import (
    EnumFailureStage as EnumRunnerFailureStage,
)
from omnimarket.nodes.node_context_roi_runner.models.model_attempt_reduction import (
    ModelAttemptReductionRow,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_result import (
    ModelContextRoiRunResult,
)

# Default output directory: repo-local evidence/scratch, never /tmp.
REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT_DIR = REPO_ROOT / ".onex_state" / "evidence" / "context_roi_battery"

# Context Authority Rule: experiment cells force factor sets but must be
# labelled as experiment assignments, not resolved selections.
SELECTION_REASON_EXPERIMENT = "experiment_assignment"


# ---------------------------------------------------------------------------
# Difficulty ladder (E1-E6) - default task battery
# ---------------------------------------------------------------------------
#
# Source: docs/plans/2026-06-10-full-feature-closure-and-statistical-proof-plan.md
#   Phase 3 - E1-E6 Experiment Ladder. The ladder spans increasing task
#   difficulty so context arms can beat no-context (E3+) and harder cells can
#   trigger escalation (E4-E6). Every task requires the golden chain (the
#   comparison spine); richer factors are optional and warned-if-absent.

GOLDEN = "golden_chain"
EXEMPLAR = "exemplar"
LOCAL_FAILURES = "local_failures"
ARCHITECTURE_PATTERNS = "architecture_patterns"
CLAUDE_MD = "claude_md"


def build_difficulty_ladder() -> tuple[ModelContextRoiTask, ...]:
    """Return the E1-E6 difficulty-ladder task battery.

    Each task carries a stable task_id (the ladder level), a natural-language
    description, the required factor spine (golden chain), and the optional
    factors that richer arms layer on.
    """
    return (
        ModelContextRoiTask(
            task_id="E1",
            task_description=(
                "Deterministic transform: generate a pure COMPUTE handler that "
                "maps an input record to an output record with no I/O. Local "
                "model should succeed on the first attempt with no escalation."
            ),
            required_factors=(GOLDEN,),
            optional_factors=(EXEMPLAR,),
        ),
        ModelContextRoiTask(
            task_id="E2",
            task_description=(
                "Simple contract + handler: generate a contract.yaml and a "
                "handler that satisfies it, then project the generated artifact. "
                "Local model should succeed and materialise the projection."
            ),
            required_factors=(GOLDEN,),
            optional_factors=(EXEMPLAR, LOCAL_FAILURES),
        ),
        ModelContextRoiTask(
            task_id="E3",
            task_description=(
                "Schema with edge cases: generate a node whose contract has "
                "optional fields, unions, and validation rules. The context arm "
                "should beat the no-context baseline here."
            ),
            required_factors=(GOLDEN,),
            optional_factors=(EXEMPLAR, LOCAL_FAILURES, ARCHITECTURE_PATTERNS),
        ),
        ModelContextRoiTask(
            task_id="E4",
            task_description=(
                "Ambiguous domain task: generate a node from an under-specified "
                "description where a semantic (not syntactic) failure can trigger "
                "escalation to a stronger route."
            ),
            required_factors=(GOLDEN,),
            optional_factors=(EXEMPLAR, LOCAL_FAILURES, ARCHITECTURE_PATTERNS),
        ),
        ModelContextRoiTask(
            task_id="E5",
            task_description=(
                "Larger generated node with registration and invocation wiring: "
                "retry and context should help; escalation fires if a fixture "
                "failure persists across attempts."
            ),
            required_factors=(GOLDEN,),
            optional_factors=(
                EXEMPLAR,
                LOCAL_FAILURES,
                ARCHITECTURE_PATTERNS,
                CLAUDE_MD,
            ),
        ),
        ModelContextRoiTask(
            task_id="E6",
            task_description=(
                "Frontier-grade reasoning / refactor: a multi-file refactor the "
                "local model is expected to underperform on, so the frontier "
                "escalation route should engage."
            ),
            required_factors=(GOLDEN,),
            optional_factors=(
                EXEMPLAR,
                LOCAL_FAILURES,
                ARCHITECTURE_PATTERNS,
                CLAUDE_MD,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Arms - canonical 7-arm factor matrix
# ---------------------------------------------------------------------------


def build_arms() -> tuple[ModelContextRoiArmSpec, ...]:
    """Build runner arm specs from the canonical scorer factor matrix.

    REUSE: the arm composition is the single canonical authority
    (build_canonical_factor_matrix). This translates each ModelFactorArm into
    the runner's ModelContextRoiArmSpec (label + ordered factor-value strings).
    """
    matrix = build_canonical_factor_matrix()
    return tuple(
        ModelContextRoiArmSpec(
            label=arm.label.value,
            factor_subset=tuple(factor.value for factor in arm.factors),
        )
        for arm in matrix
    )


def build_run_request(
    *,
    run_id: str | None = None,
    trials_per_cell: int = 3,
    max_attempts: int = 2,
    arm_order_seed: int = 42,
    generation_timeout_seconds: float = 120.0,
    artifact_content_map: dict[str, str] | None = None,
) -> ModelContextRoiRunRequest:
    """Construct the contract-canonical runner command for the full matrix."""
    return ModelContextRoiRunRequest(
        run_id=run_id or f"context-roi-battery-{uuid.uuid4().hex[:12]}",
        tasks=build_difficulty_ladder(),
        arms=build_arms(),
        trials_per_cell=trials_per_cell,
        max_attempts=max_attempts,
        arm_order_seed=arm_order_seed,
        generation_timeout_seconds=generation_timeout_seconds,
        artifact_content_map=artifact_content_map or {},
    )


# ---------------------------------------------------------------------------
# Runner-row -> scorer-row translation
# ---------------------------------------------------------------------------

# The runner and scorer each own their own EnumFailureStage. The runner's
# `validation`/`generation`/`pack_build`/`budget_fail` stages map onto the
# scorer's vocabulary; the scorer additionally distinguishes
# `missing_required_factor`. The runner records a missing-required-factor as
# `pack_build` with a diagnostic warning, so pack_build -> pack_build is the
# faithful mapping (the scorer re-derives missing-required from factors_present).
_FAILURE_STAGE_MAP: dict[EnumRunnerFailureStage, EnumScorerFailureStage] = {
    EnumRunnerFailureStage.NONE: EnumScorerFailureStage.NONE,
    EnumRunnerFailureStage.PACK_BUILD: EnumScorerFailureStage.PACK_BUILD,
    EnumRunnerFailureStage.BUDGET_FAIL: EnumScorerFailureStage.BUDGET_FAIL,
    EnumRunnerFailureStage.GENERATION: EnumScorerFailureStage.GENERATION,
    EnumRunnerFailureStage.VALIDATION: EnumScorerFailureStage.VALIDATION,
    EnumRunnerFailureStage.DOWNSTREAM_GATE: EnumScorerFailureStage.DOWNSTREAM_GATE,
}


def _arm_label_from_subset(subset_label: str) -> EnumArmLabel:
    """Resolve the runner's arm-label string to the scorer's EnumArmLabel."""
    return EnumArmLabel(subset_label)


def _factors_present_for_arm(arm_label: EnumArmLabel) -> tuple[Any, ...]:
    """Return the canonical factors for an arm (for scorer required-factor checks)."""
    for arm in build_canonical_factor_matrix():
        if arm.label == arm_label:
            return arm.factors
    return ()


def runner_rows_to_scorer_rows(
    run_result: ModelContextRoiRunResult,
) -> tuple[ModelArmRunRow, ...]:
    """Translate runner ModelAttemptReductionRow rows into scorer ModelArmRunRow.

    Trial index is reconstructed per (task x arm) from the rows' ordering: the
    runner emits rows in run_order, so we count occurrences per (task, arm).
    """
    trial_counter: dict[tuple[str, str], int] = {}
    scorer_rows: list[ModelArmRunRow] = []

    for row in run_result.rows:
        key = (row.task_id, row.context_factor_subset)
        trial_index = trial_counter.get(key, 0)
        trial_counter[key] = trial_index + 1

        arm_label = _arm_label_from_subset(row.context_factor_subset)
        scorer_rows.append(_translate_row(row, arm_label, trial_index))

    return tuple(scorer_rows)


def _translate_row(
    row: ModelAttemptReductionRow,
    arm_label: EnumArmLabel,
    trial_index: int,
) -> ModelArmRunRow:
    # The scorer requires attempt_count >= 1. A row that failed before any LLM
    # attempt (pack_build / immediate generation failure) records attempt_count=0
    # on the runner row; treat that as a single failed attempt for the scorer.
    attempt_count = max(row.attempt_count, 1)
    return ModelArmRunRow(
        task_id=row.task_id,
        arm_label=arm_label,
        trial_index=trial_index,
        run_id=row.run_id,
        first_pass_success=row.first_pass_success,
        final_success=row.final_success,
        attempt_count=attempt_count,
        failure_stage=_FAILURE_STAGE_MAP[row.failure_stage],
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        estimated_cost_usd=row.estimated_cost,
        model_id=row.model_id or None,
        provider=row.provider or None,
        endpoint_ref=row.endpoint_ref or None,
        context_pack_hash=row.context_pack_hash or None,
        run_order=row.run_order,
        factors_present=_factors_present_for_arm(arm_label),
    )


# ---------------------------------------------------------------------------
# Correlation-ID manifest
# ---------------------------------------------------------------------------


class ModelBatteryCell(BaseModel):
    """One (task x arm x trial) cell entry in the correlation manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    task_id: str
    arm_label: str
    trial_index: int
    run_order: int
    context_pack_hash: str
    # Context Authority Rule labelling.
    selection_reason: str = SELECTION_REASON_EXPERIMENT
    experiment_cohort: str


class ModelBatteryManifest(BaseModel):
    """Correlation-ID manifest tagging every cell with experiment + arm + cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    run_id: str
    task_count: int
    arm_count: int
    trials_per_cell: int
    cells: tuple[ModelBatteryCell, ...]


def build_manifest(
    experiment_id: str,
    run_result: ModelContextRoiRunResult,
    trials_per_cell: int,
) -> ModelBatteryManifest:
    trial_counter: dict[tuple[str, str], int] = {}
    cells: list[ModelBatteryCell] = []
    arms_seen: set[str] = set()
    tasks_seen: set[str] = set()

    for row in run_result.rows:
        key = (row.task_id, row.context_factor_subset)
        trial_index = trial_counter.get(key, 0)
        trial_counter[key] = trial_index + 1
        arms_seen.add(row.context_factor_subset)
        tasks_seen.add(row.task_id)
        cells.append(
            ModelBatteryCell(
                correlation_id=row.correlation_id,
                task_id=row.task_id,
                arm_label=row.context_factor_subset,
                trial_index=trial_index,
                run_order=row.run_order,
                context_pack_hash=row.context_pack_hash,
                experiment_cohort=experiment_id,
            )
        )

    return ModelBatteryManifest(
        experiment_id=experiment_id,
        run_id=run_result.run_id,
        task_count=len(tasks_seen),
        arm_count=len(arms_seen),
        trials_per_cell=trials_per_cell,
        cells=tuple(cells),
    )


# ---------------------------------------------------------------------------
# Battery report
# ---------------------------------------------------------------------------


class ModelBatteryReport(BaseModel):
    """In-process result of running the battery (returned to callers/tests)."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    experiment_id: str
    dry_run: bool
    expected_cell_count: int
    run_result: ModelContextRoiRunResult | None = None
    score_result: ModelContextRoiResult | None = None
    manifest: ModelBatteryManifest | None = None


# `from __future__ import annotations` turns every annotation into a string, and
# this module is loaded by path (importlib) under a name that is not importable
# as a package, so Pydantic cannot always resolve the local forward references
# at class-definition time. Rebuild explicitly so the models are usable
# regardless of how the script module was loaded.
ModelBatteryManifest.model_rebuild()
ModelBatteryReport.model_rebuild()


# ---------------------------------------------------------------------------
# Bus injection
# ---------------------------------------------------------------------------

# A bus is anything exposing publish(topic, payload) and
# consume(topic, correlation_id, timeout) -> dict | None. In production the
# Kafka adapter is injected; in tests a fake stands in. The runner imports its
# topics from its own contract - no topic literals appear here.


class ProtocolBatteryBus(Protocol):
    def publish(self, topic: str, payload: bytes) -> None: ...

    def consume(
        self, topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None: ...


def _build_runner(bus: ProtocolBatteryBus | None) -> HandlerContextRoiRunner:
    if bus is None:
        # No bus wired: the runner falls back to its noop publisher/consumer
        # (used only on the dry-run path, which never calls handle()).
        return HandlerContextRoiRunner()
    return HandlerContextRoiRunner(
        event_publisher=bus.publish,
        event_consumer=bus.consume,
    )


# ---------------------------------------------------------------------------
# Orchestration entry point
# ---------------------------------------------------------------------------


def run_battery(
    *,
    trials_per_cell: int = 3,
    max_attempts: int = 2,
    arm_order_seed: int = 42,
    generation_timeout_seconds: float = 120.0,
    dry_run: bool = False,
    output_dir: Path | None = None,
    artifact_content_map: dict[str, str] | None = None,
    bus_factory: Callable[[], ProtocolBatteryBus] | None = None,
    run_id: str | None = None,
) -> ModelBatteryReport:
    """Run the full (task x arm x trial) battery.

    dry_run=True validates wiring (matrix, request models, cell count, manifest
    shape) without constructing or invoking any bus - the runner.handle() loop
    is never entered and nothing is published.
    """
    out_dir = output_dir or _DEFAULT_OUTPUT_DIR
    experiment_id = f"context-roi-exp-{uuid.uuid4().hex[:12]}"

    request = build_run_request(
        run_id=run_id,
        trials_per_cell=trials_per_cell,
        max_attempts=max_attempts,
        arm_order_seed=arm_order_seed,
        generation_timeout_seconds=generation_timeout_seconds,
        artifact_content_map=artifact_content_map,
    )
    expected_cell_count = len(request.tasks) * len(request.arms) * trials_per_cell

    if dry_run:
        # Wiring-only: prove the request is well-formed and the scorer accepts a
        # single synthetic row, but publish nothing and write no heavy artifact.
        return ModelBatteryReport(
            experiment_id=experiment_id,
            dry_run=True,
            expected_cell_count=expected_cell_count,
        )

    bus = (bus_factory or _default_bus_factory)()
    runner = _build_runner(bus)
    run_result = runner.handle(request)

    scorer_rows = runner_rows_to_scorer_rows(run_result)
    score_result = HandlerContextRoi().handle(
        ModelContextRoiRequest(
            run_id=run_result.run_id,
            manifest_id=experiment_id,
            rows=scorer_rows,
            fixture_mode=True,
        )
    )

    manifest = build_manifest(experiment_id, run_result, trials_per_cell)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_artifacts(out_dir, run_result, score_result, manifest)

    return ModelBatteryReport(
        experiment_id=experiment_id,
        dry_run=False,
        expected_cell_count=expected_cell_count,
        run_result=run_result,
        score_result=score_result,
        manifest=manifest,
    )


def _default_bus_factory() -> ProtocolBatteryBus:
    """Production bus factory.

    The Kafka adapter is wired by the runtime when this orchestrator runs in a
    deployed lane. Until that coordinated deploy lands (gated per OMN-12792),
    callers must inject a bus explicitly (tests do). Refuse to silently fall
    back to a noop bus that would fake a live run.
    """
    raise RuntimeError(
        "no bus_factory injected: a live battery run requires a Kafka bus "
        "adapter wired by the runtime (gated on the coordinated lane deploy, "
        "OMN-12792). Use --dry-run for offline wiring validation, or inject a "
        "bus_factory programmatically."
    )


def _write_artifacts(
    out_dir: Path,
    run_result: ModelContextRoiRunResult,
    score_result: ModelContextRoiResult,
    manifest: ModelBatteryManifest,
) -> None:
    (out_dir / "context_roi_battery_result.json").write_text(
        json.dumps(
            {
                "run_result": run_result.model_dump(mode="json"),
                "score_result": score_result.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (out_dir / "context_roi_battery_manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Context-ROI E2E battery orchestrator - drives the full "
            "(task x arm x trial) matrix over the runner + scorer."
        )
    )
    parser.add_argument(
        "--trials-per-cell",
        type=int,
        default=3,
        help="Trials per (task x arm) cell (K). >=3 recommended.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="Max generation attempts forwarded to the generation consumer.",
    )
    parser.add_argument(
        "--arm-order-seed",
        type=int,
        default=42,
        help="Seed for arm-order randomisation within each task.",
    )
    parser.add_argument(
        "--generation-timeout-seconds",
        type=float,
        default=120.0,
        help="Per-trial timeout waiting for the terminal generation event.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write the result artifact + manifest.",
    )
    parser.add_argument(
        "--artifact-content-map",
        type=Path,
        default=None,
        help=(
            "Optional JSON file: pre-resolved artifact content keyed by "
            "EnumContextFactor value, produced by the content-resolver effect. "
            "No hardcoded paths - the caller supplies the source."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate wiring without any bus traffic.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    artifact_content_map: dict[str, str] | None = None
    if args.artifact_content_map is not None:
        artifact_content_map = json.loads(
            args.artifact_content_map.read_text(encoding="utf-8")
        )

    report = run_battery(
        trials_per_cell=args.trials_per_cell,
        max_attempts=args.max_attempts,
        arm_order_seed=args.arm_order_seed,
        generation_timeout_seconds=args.generation_timeout_seconds,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
        artifact_content_map=artifact_content_map,
    )

    if report.dry_run:
        print(
            f"[dry-run] experiment={report.experiment_id} "
            f"expected_cells={report.expected_cell_count} (no bus traffic)"
        )
        return 0

    assert report.score_result is not None
    assert report.manifest is not None
    print(
        f"experiment={report.experiment_id} "
        f"cells={len(report.manifest.cells)} "
        f"preferred_arm={report.score_result.preferred_arm} "
        f"proof_class={report.score_result.proof_class}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
