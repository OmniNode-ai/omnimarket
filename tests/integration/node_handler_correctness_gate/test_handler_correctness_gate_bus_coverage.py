# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-output COMPUTE coverage for node_handler_correctness_gate,
driven over the canonical in-memory bus.

OMN-13674 (cluster wave-D-projection-correctness-verification, archetype
compute). ``HandlerCorrectnessGate`` is driven end to end over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture +
``LocalRuntimeBusAdapter``): a ``ModelCorrectnessCheckRequest`` lands on the
declared command topic ``onex.cmd.omnimarket.handler-correctness-check.v1`` and
the terminal ``ModelCorrectnessCheckResult`` is auto-published onto the declared
completed topic ``onex.evt.omnimarket.handler-correctness-result.v1``. No live
Kafka / ``.201``.

COMPUTE DoD covered:
  * every declared output field asserted off the terminal event
    (``handler_id``, ``score``, ``passed``, ``total_entries``,
    ``correct_entries``, ``failures``, ``eval_set_name``) — never a
    "returned without raising";
  * both terminal verdicts reached: ``passed=True`` (score >= min_score) and
    ``passed=False`` (score < min_score);
  * every ``_score_entry`` scoring-method branch:
    ``EnumScoringMethod.EXACT_MATCH``, ``CONTAINS``, ``STARTS_WITH``;
  * the empty-eval-set edge (``total_entries == 0`` -> score 0.0, not passed);
  * the missing-actual-output branch (fewer actuals than entries -> "" scored);
  * a negative control: a known-bad actual output MUST land in ``failures`` with
    the mismatched expected/actual recorded — the gate never scores it correct;
  * idempotency: identical input yields an identical terminal event.

Honest finding: ``_score_entry`` has a trailing ``return False`` default branch
for an unknown scoring method, but ``EnumScoringMethod`` is a closed 3-member
StrEnum validated at the model boundary, so that default is unreachable through
the declared contract surface. It is therefore not (and cannot be) covered here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_handler_correctness_gate.handlers.handler_correctness_gate import (
    HandlerCorrectnessGate,
)
from omnimarket.nodes.node_handler_correctness_gate.models.enums import (
    EnumScoringMethod,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_correctness_check_request import (
    ModelCorrectnessCheckRequest,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_correctness_check_result import (
    ModelCorrectnessCheckResult,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_entry import (
    ModelEvalEntry,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_set import (
    ModelEvalSet,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.handler-correctness-check.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.handler-correctness-result.v1"


async def _drive(
    bus: Any, command: ModelCorrectnessCheckRequest, *, group: str
) -> ModelCorrectnessCheckResult:
    """Publish the command onto the declared topic and read the terminal event
    back off the declared completed topic — the whole flow transits the bus."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerCorrectnessGate(),
        handler_name="handler-correctness-gate",
        input_model_cls=ModelCorrectnessCheckRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id=group,
    )
    await bus.publish(TOPIC_COMMAND, None, command.model_dump_json().encode("utf-8"))
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    assert completed[-1].topic == "onex.evt.omnimarket.handler-correctness-result.v1"
    return ModelCorrectnessCheckResult.model_validate(json.loads(completed[-1].value))


def _request(
    *,
    entries: tuple[ModelEvalEntry, ...],
    actuals: tuple[str, ...],
    min_score: float = 0.85,
    name: str = "eval-set",
    handler_id: str = "handler-under-test",
) -> ModelCorrectnessCheckRequest:
    return ModelCorrectnessCheckRequest(
        handler_id=handler_id,
        eval_set=ModelEvalSet(entries=entries, min_score=min_score, name=name),
        actual_outputs=actuals,
        correlation_id="corr-gate",
    )


# ---------------------------------------------------------------------------
# passed=True — every scoring-method branch scores correct; perfect score.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_all_scoring_methods_pass_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        entries = (
            ModelEvalEntry(
                input="q1", expected="exact", scoring=EnumScoringMethod.EXACT_MATCH
            ),
            ModelEvalEntry(
                input="q2", expected="needle", scoring=EnumScoringMethod.CONTAINS
            ),
            ModelEvalEntry(
                input="q3", expected="pre", scoring=EnumScoringMethod.STARTS_WITH
            ),
        )
        actuals = ("exact", "a needle in text", "prefixed")
        result = await _drive(
            bus, _request(entries=entries, actuals=actuals), group="gate-pass"
        )
        # Every declared output field asserted.
        assert result.handler_id == "handler-under-test"
        assert result.total_entries == 3
        assert result.correct_entries == 3
        assert result.score == 1.0
        assert result.passed is True
        assert result.failures == ()
        assert result.eval_set_name == "eval-set"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# passed=False — every scoring method mis-scores; negative control failures.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_all_scoring_methods_fail_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        entries = (
            ModelEvalEntry(
                input="q1", expected="exact", scoring=EnumScoringMethod.EXACT_MATCH
            ),
            ModelEvalEntry(
                input="q2", expected="needle", scoring=EnumScoringMethod.CONTAINS
            ),
            ModelEvalEntry(
                input="q3", expected="pre", scoring=EnumScoringMethod.STARTS_WITH
            ),
        )
        # Known-bad actuals: none match their scoring rule.
        actuals = ("not-exact", "haystack only", "unprefixed")
        result = await _drive(
            bus, _request(entries=entries, actuals=actuals), group="gate-fail"
        )
        assert result.total_entries == 3
        assert result.correct_entries == 0
        assert result.score == 0.0
        assert result.passed is False
        # Negative control: every entry recorded as a failure with the mismatch.
        assert len(result.failures) == 3
        by_index = {f.entry_index: f for f in result.failures}
        assert by_index[0].expected == "exact"
        assert by_index[0].actual == "not-exact"
        assert by_index[0].scoring == EnumScoringMethod.EXACT_MATCH
        assert by_index[1].scoring == EnumScoringMethod.CONTAINS
        assert by_index[2].scoring == EnumScoringMethod.STARTS_WITH
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Partial score at the min_score boundary — passed True vs False by threshold.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_partial_score_threshold_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        entries = (
            ModelEvalEntry(input="q1", expected="a"),
            ModelEvalEntry(input="q2", expected="b"),
        )
        # 1/2 correct = 0.5. min_score 0.5 -> passed; min_score 0.6 -> not.
        passing = await _drive(
            bus,
            _request(entries=entries, actuals=("a", "WRONG"), min_score=0.5),
            group="gate-thresh-pass",
        )
        assert passing.score == 0.5
        assert passing.passed is True
        assert passing.correct_entries == 1
        assert len(passing.failures) == 1
    finally:
        await bus.close()

    bus2 = type(integration_event_bus)(
        environment="integration-test", group="omnimarket-integration"
    )
    await bus2.start()
    try:
        entries = (
            ModelEvalEntry(input="q1", expected="a"),
            ModelEvalEntry(input="q2", expected="b"),
        )
        failing = await _drive(
            bus2,
            _request(entries=entries, actuals=("a", "WRONG"), min_score=0.6),
            group="gate-thresh-fail",
        )
        assert failing.score == 0.5
        assert failing.passed is False
    finally:
        await bus2.close()


# ---------------------------------------------------------------------------
# Empty eval set edge — total_entries 0 -> score 0.0, not passed, no failures.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_empty_eval_set_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            _request(entries=(), actuals=(), name="empty-set"),
            group="gate-empty",
        )
        assert result.total_entries == 0
        assert result.correct_entries == 0
        assert result.score == 0.0
        assert result.passed is False
        assert result.failures == ()
        assert result.eval_set_name == "empty-set"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Missing-actual branch — fewer actuals than entries scores "" (a failure).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_missing_actual_scored_empty_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        entries = (
            ModelEvalEntry(input="q1", expected="present"),
            ModelEvalEntry(input="q2", expected="also"),
        )
        # Only one actual supplied; entry index 1 falls back to "" and fails.
        result = await _drive(
            bus,
            _request(entries=entries, actuals=("present",)),
            group="gate-missing-actual",
        )
        assert result.total_entries == 2
        assert result.correct_entries == 1
        assert len(result.failures) == 1
        assert result.failures[0].entry_index == 1
        assert result.failures[0].actual == ""
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Idempotency — identical input yields an identical terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_deterministic_identical_input_over_bus(
    integration_event_bus: Any,
) -> None:
    bus_factory = type(integration_event_bus)
    entries = (
        ModelEvalEntry(input="q1", expected="x", scoring=EnumScoringMethod.CONTAINS),
        ModelEvalEntry(input="q2", expected="y"),
    )
    command = _request(entries=entries, actuals=("xyz", "nope"))
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            result = await _drive(bus, command, group="gate-idem")
            payloads.append(result.model_dump_json())
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
