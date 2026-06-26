# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerRegressionTestOrchestrator (OMN-13616).

Proves the DoD: deterministic replay from a recorded corpus, emission of the
canonical ModelExperimentResult (OMN-13613), and the ORCHESTRATOR archetype
output constraint (events only, no result/projections).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)

from omnimarket.nodes.node_regression_test_orchestrator.handlers.handler_regression_test_orchestrator import (
    TOPIC_REGRESSION_COMPLETED,
    HandlerRegressionTestOrchestrator,
    aggregate_experiment_result,
    replay_results,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_replay_entry import (
    ModelRegressionReplayEntry,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_suite_start import (
    ModelRegressionSuiteStart,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_task import (
    REGRESSION_TASKS,
    EnumRegressionDifficulty,
    ModelRegressionTask,
)

_EXP_ID = UUID("11111111-1111-1111-1111-111111111111")
_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
_CORR_ID = UUID("33333333-3333-3333-3333-333333333333")


def _task(task_id: str) -> ModelRegressionTask:
    return ModelRegressionTask(
        task_id=task_id,
        description=f"desc {task_id}",
        expected_difficulty=EnumRegressionDifficulty.SIMPLE,
        baseline_pass_rate=1.0,
        baseline_avg_attempts=1.0,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/x.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    )


def _start(
    *,
    tasks: tuple[ModelRegressionTask, ...],
    corpus: tuple[ModelRegressionReplayEntry, ...],
    samples_per_task: int = 1,
) -> ModelRegressionSuiteStart:
    return ModelRegressionSuiteStart(
        experiment_id=_EXP_ID,
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
        runtime_identity="replay/test",
        samples_per_task=samples_per_task,
        tasks=tasks,
        replay_corpus=corpus,
    )


@pytest.mark.unit
def test_replay_passed_iff_recorded_output_nonempty() -> None:
    """SEA _run_replay semantics: passed == bool(output) for the attempt-1 entry."""
    tasks = (_task("reg-001"), _task("reg-002"), _task("reg-003"))
    corpus = (
        ModelRegressionReplayEntry(
            task_id="reg-001", attempt=1, output="def f(): pass"
        ),
        ModelRegressionReplayEntry(task_id="reg-002", attempt=1, output=""),
        # reg-003 has no entry at all -> failed
    )
    results = replay_results(_start(tasks=tasks, corpus=corpus))
    by_id = {r.task_id: r for r in results}
    assert by_id["reg-001"].passed is True
    assert by_id["reg-002"].passed is False
    assert by_id["reg-003"].passed is False
    assert all(r.tier_used == "replay" for r in results)
    assert all(r.attempt_count == 1 for r in results)


@pytest.mark.unit
def test_replay_only_consults_attempt_one() -> None:
    """A passing attempt-2 entry does NOT pass the task; only attempt 1 is read."""
    tasks = (_task("reg-001"),)
    corpus = (
        ModelRegressionReplayEntry(task_id="reg-001", attempt=2, output="late pass"),
    )
    results = replay_results(_start(tasks=tasks, corpus=corpus))
    assert results[0].passed is False


@pytest.mark.unit
def test_samples_per_task_controls_provisional_flag() -> None:
    tasks = (_task("reg-001"),)
    corpus = (ModelRegressionReplayEntry(task_id="reg-001", attempt=1, output="ok"),)
    one = replay_results(_start(tasks=tasks, corpus=corpus, samples_per_task=1))
    many = replay_results(_start(tasks=tasks, corpus=corpus, samples_per_task=3))
    assert one[0].provisional is True
    assert many[0].provisional is False
    assert many[0].samples_per_task == 3


@pytest.mark.unit
def test_replay_is_deterministic() -> None:
    """Two replays of the same start produce byte-identical experiment results."""
    tasks = (_task("reg-001"), _task("reg-002"))
    corpus = (
        ModelRegressionReplayEntry(task_id="reg-001", attempt=1, output="a"),
        ModelRegressionReplayEntry(task_id="reg-002", attempt=1, output=""),
    )
    start = _start(tasks=tasks, corpus=corpus)
    first = aggregate_experiment_result(start, replay_results(start))
    second = aggregate_experiment_result(start, replay_results(start))
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.unit
def test_aggregate_all_pass_is_completed_score_one() -> None:
    tasks = (_task("reg-001"), _task("reg-002"))
    corpus = (
        ModelRegressionReplayEntry(task_id="reg-001", attempt=1, output="a"),
        ModelRegressionReplayEntry(task_id="reg-002", attempt=1, output="b"),
    )
    start = _start(tasks=tasks, corpus=corpus)
    result = aggregate_experiment_result(start, replay_results(start))
    assert result.status is EnumExperimentStatus.COMPLETED
    assert result.score.value == 1.0
    assert result.experiment_type is EnumExperimentType.REGRESSION_TEST
    assert result.experiment_id == _EXP_ID
    assert result.run_id == _RUN_ID
    assert result.correlation_id == _CORR_ID
    assert result.evidence_ref.evidence_id == _RUN_ID


@pytest.mark.unit
def test_aggregate_partial_pass_is_failed_with_fractional_score() -> None:
    tasks = (_task("reg-001"), _task("reg-002"), _task("reg-003"), _task("reg-004"))
    corpus = (
        ModelRegressionReplayEntry(task_id="reg-001", attempt=1, output="a"),
        ModelRegressionReplayEntry(task_id="reg-003", attempt=1, output="c"),
    )
    start = _start(tasks=tasks, corpus=corpus)
    result = aggregate_experiment_result(start, replay_results(start))
    assert result.status is EnumExperimentStatus.FAILED
    assert result.score.value == 0.5  # 2 of 4 passed
    assert result.cost.cost_usd == 0  # pure replay, zero cost


@pytest.mark.unit
def test_handle_emits_canonical_result_on_terminal_topic() -> None:
    handler = HandlerRegressionTestOrchestrator()
    start = _start(
        tasks=(_task("reg-001"),),
        corpus=(ModelRegressionReplayEntry(task_id="reg-001", attempt=1, output="ok"),),
    )
    envelope: ModelEventEnvelope[ModelRegressionSuiteStart] = ModelEventEnvelope(
        payload=start,
        correlation_id=_CORR_ID,
        event_type=start.tasks[0].task_id,
    )
    output = asyncio.run(handler.handle(envelope))

    # ORCHESTRATOR archetype constraint: events only, never result/projections.
    assert output.result is None
    assert len(output.events) == 1
    emitted = output.events[0]
    assert emitted.event_type == TOPIC_REGRESSION_COMPLETED
    assert isinstance(emitted.payload, ModelExperimentResult)
    assert emitted.payload.experiment_type is EnumExperimentType.REGRESSION_TEST
    assert emitted.payload.status is EnumExperimentStatus.COMPLETED


@pytest.mark.unit
def test_handle_accepts_mapping_payload_on_run_node_path() -> None:
    """onex run-node delivers a mapping payload; correlation_id is injected."""
    handler = HandlerRegressionTestOrchestrator()
    payload = {
        "experiment_id": str(_EXP_ID),
        "run_id": str(_RUN_ID),
        "runtime_identity": "replay/run-node",
        "tasks": [t.model_dump(mode="json") for t in (_task("reg-001"),)],
        "replay_corpus": [
            {"task_id": "reg-001", "attempt": 1, "output": "ok"},
        ],
    }
    envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
        payload=payload,
        correlation_id=_CORR_ID,
        event_type="onex.cmd.omnimarket.regression-suite-start.v1",
    )
    output = asyncio.run(handler.handle(envelope))
    assert isinstance(output.events[0].payload, ModelExperimentResult)
    assert output.events[0].payload.correlation_id == _CORR_ID


@pytest.mark.unit
def test_default_suite_is_seven_baseline_tasks() -> None:
    """Default start replays the absorbed SEA seven-task suite."""
    assert len(REGRESSION_TASKS) == 7
    start = ModelRegressionSuiteStart()
    assert start.tasks == REGRESSION_TASKS


@pytest.mark.unit
def test_empty_suite_scores_zero_and_fails_without_zero_division() -> None:
    start = _start(tasks=(), corpus=())
    result = aggregate_experiment_result(start, replay_results(start))
    assert result.score.value == 0.0
    assert result.status is EnumExperimentStatus.FAILED
