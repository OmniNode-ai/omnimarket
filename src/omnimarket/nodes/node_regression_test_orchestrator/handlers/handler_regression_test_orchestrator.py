# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression-test ORCHESTRATOR handler (OMN-13616).

Canonical home for the SEA regression suite. The SEA ``regression/runner.py``
had two paths: ``_run_live`` (called the imperative DelegationExecutor) and
``_run_replay`` (replayed cached LLM responses, deterministic, CI mode). Only
the **replay** path is migrated: the canonical node replays a recorded event
corpus deterministically and never does live generation, never does I/O.

``handle(envelope)`` is pure and deterministic:

  1. Coerce the payload into a typed :class:`ModelRegressionSuiteStart` carrying
     the task suite + recorded replay corpus.
  2. For each task, look up the recorded attempt-1 output (SEA replay keyed on
     ``(task_id, attempt)``); ``passed = bool(output)`` — exactly the SEA
     ``_run_replay`` semantics. ``samples_per_task == 1`` marks results
     provisional (single stochastic run).
  3. Aggregate the per-task outcomes into the single canonical
     :class:`ModelExperimentResult` (OMN-13613) and return it as the terminal
     event the runtime publishes. No I/O, no live providers, no in-process loop.

An ORCHESTRATOR returns ``events``/``intents`` only — never ``result`` — so the
aggregated experiment result rides the terminal event envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.experiment.model_experiment_cost import ModelExperimentCost
from omnibase_core.models.experiment.model_experiment_evidence_ref import (
    ModelExperimentEvidenceRef,
)
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)
from omnibase_core.models.experiment.model_experiment_score import ModelExperimentScore

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_result import (
    ModelRegressionResult,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_suite_start import (
    ModelRegressionSuiteStart,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_task import (
    ModelRegressionTask,
)

HANDLER_ID = "regression-test-orchestrator"

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_PUBLISH = contract_publish_topics(_CONTRACT)


def _topic_with_suffix(suffix: str) -> str:
    """Resolve exactly one contract publish topic ending with ``suffix``."""
    matches = [t for t in _PUBLISH if t.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.publish_topics "
            f"topic ending in {suffix!r}; found {matches}"
        )
    return matches[0]


TOPIC_REGRESSION_COMPLETED = _topic_with_suffix("regression-suite-completed.v1")


def replay_results(
    start: ModelRegressionSuiteStart,
) -> tuple[ModelRegressionResult, ...]:
    """Deterministically replay each task against the recorded corpus.

    Preserves SEA ``_run_replay`` semantics: the corpus is keyed ``(task_id,
    attempt)`` and only attempt 1 is consulted; ``passed = bool(output)``. A task
    with no matching entry replays as a failed task (empty output).
    """
    provisional = start.samples_per_task == 1
    replay_map = {(e.task_id, e.attempt): e.output for e in start.replay_corpus}
    results: list[ModelRegressionResult] = []
    for task in start.tasks:
        output = replay_map.get((task.task_id, 1), "")
        results.append(
            ModelRegressionResult(
                task_id=task.task_id,
                passed=bool(output),
                attempt_count=1,
                tier_used="replay",
                samples_per_task=start.samples_per_task,
                provisional=provisional,
            )
        )
    return tuple(results)


def aggregate_experiment_result(
    start: ModelRegressionSuiteStart,
    results: tuple[ModelRegressionResult, ...],
) -> ModelExperimentResult:
    """Fold per-task replay outcomes into the canonical ModelExperimentResult.

    ``score`` is the pass fraction over the replayed tasks (a fixed-suite replay
    is never empty, but an explicitly empty suite scores 0.0 and is reported
    FAILED rather than dividing by zero). ``cost`` sums the per-task USD cost
    (0.0 for a pure replay). ``status`` is COMPLETED when every task passed,
    FAILED otherwise.
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    score_value = (passed / total) if total else 0.0
    cost_total = sum((Decimal(str(r.total_cost_usd)) for r in results), Decimal("0"))
    status = (
        EnumExperimentStatus.COMPLETED
        if total and passed == total
        else EnumExperimentStatus.FAILED
    )
    return ModelExperimentResult(
        experiment_id=start.experiment_id,
        experiment_type=EnumExperimentType.REGRESSION_TEST,
        run_id=start.run_id,
        correlation_id=start.correlation_id,
        runtime_identity=start.runtime_identity,
        score=ModelExperimentScore(value=score_value, scale_max=1.0),
        cost=ModelExperimentCost(cost_usd=cost_total),
        status=status,
        evidence_ref=ModelExperimentEvidenceRef(evidence_id=start.run_id),
    )


class HandlerRegressionTestOrchestrator:
    """Canonical orchestrator: deterministic regression replay -> experiment result."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Replay the regression suite and emit the canonical experiment result."""
        correlation_id = envelope.correlation_id or uuid4()
        start = _coerce_start(envelope.payload, correlation_id)
        results = replay_results(start)
        experiment_result = aggregate_experiment_result(start, results)
        completed: ModelEventEnvelope[ModelExperimentResult] = ModelEventEnvelope(
            payload=experiment_result,
            correlation_id=experiment_result.correlation_id,
            event_type=TOPIC_REGRESSION_COMPLETED,
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=envelope.envelope_id,
            correlation_id=experiment_result.correlation_id,
            handler_id=HANDLER_ID,
            events=(completed,),
        )


def _coerce_start(payload: Any, correlation_id: UUID) -> ModelRegressionSuiteStart:
    """Coerce a dispatch payload into the typed start command.

    On the ``onex run-node`` path the payload may arrive as a mapping; the
    runtime-injected envelope ``correlation_id`` is threaded in when the payload
    does not carry one. The default task suite + empty corpus apply when absent.
    """
    if isinstance(payload, ModelRegressionSuiteStart):
        return payload
    if isinstance(payload, ModelRegressionTask):
        raise TypeError(
            "regression-suite-start payload must be ModelRegressionSuiteStart, not "
            "a single ModelRegressionTask"
        )
    if isinstance(payload, Mapping):
        data = dict(payload)
        data.setdefault("correlation_id", str(correlation_id))
        return ModelRegressionSuiteStart.model_validate(data)
    if payload is None:
        return ModelRegressionSuiteStart(correlation_id=correlation_id)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        data = dict(model_dump(mode="json"))
        data.setdefault("correlation_id", str(correlation_id))
        return ModelRegressionSuiteStart.model_validate(data)
    raise TypeError(
        "regression-suite-start payload must be ModelRegressionSuiteStart or a "
        f"mapping; got {type(payload).__name__}"
    )


__all__ = [
    "HANDLER_ID",
    "TOPIC_REGRESSION_COMPLETED",
    "HandlerRegressionTestOrchestrator",
    "aggregate_experiment_result",
    "replay_results",
]
