# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-output COMPUTE coverage for node_candidate_pool_compute,
driven over the canonical in-memory bus.

OMN-13674 (cluster wave-kb-context-knowledge, archetype compute). This module
drives ``HandlerCandidatePool`` end to end over ``EventBusInmemory`` (via the
``integration_event_bus`` fixture + ``LocalRuntimeBusAdapter``): a
``ModelCandidatePoolRequest`` lands on the declared command topic
``onex.cmd.omnimarket.candidate-pool-requested.v1`` and the terminal
``ModelCandidatePoolResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.candidate-pool-completed.v1``. No live Kafka / ``.201``.

COMPUTE DoD covered:
  * every declared output field asserted off the terminal event (``status``,
    ``run_id``, ``ranked_candidates``, ``best_candidate_index``, ``all_valid``,
    ``summary``, ``error``) — never a "returned without raising";
  * every declared verdict class reached: ``EnumPoolStatus.OK`` (all-valid,
    mixed-validity, over-budget) and ``EnumPoolStatus.ERROR`` (insufficient
    candidates);
  * every ``_validate_schema`` branch: valid JSON matching schema, malformed
    (non-JSON), wrong root type, missing required key;
  * the ``within_budget`` true/false branch and the fitness-ranking order;
  * a negative control: a known-bad (non-JSON) candidate MUST score
    ``schema_valid=False`` and never win the pool;
  * idempotency: identical input yields an identical terminal event.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_candidate_pool_compute.handlers.handler_candidate_pool import (
    HandlerCandidatePool,
)
from omnimarket.nodes.node_candidate_pool_compute.models.model_pool_request import (
    ModelCandidatePoolRequest,
)
from omnimarket.nodes.node_candidate_pool_compute.models.model_pool_result import (
    EnumPoolStatus,
    ModelCandidatePoolResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.candidate-pool-requested.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.candidate-pool-completed.v1"

# A JSON Schema requiring an object with a "name" string field.
_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}},
}


async def _drive(
    bus: Any, command: ModelCandidatePoolRequest
) -> ModelCandidatePoolResult:
    """Publish the command onto the declared topic and read the terminal event
    back off the declared completed topic — the whole flow transits the bus."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerCandidatePool(),
        handler_name="candidate-pool",
        input_model_cls=ModelCandidatePoolRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-candidate-pool-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=command.model_dump_json().encode("utf-8")
    )
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    assert completed[-1].topic == "onex.evt.omnimarket.candidate-pool-completed.v1"
    return ModelCandidatePoolResult.model_validate(json.loads(completed[-1].value))


# ---------------------------------------------------------------------------
# OK verdict — all candidates schema-valid and within budget.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_all_valid_within_budget_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelCandidatePoolRequest(
                candidates=['{"name": "a"}', '{"name": "bb"}'],
                target_schema=_SCHEMA,
                max_loc=10,
                run_id="run-ok",
            ),
        )
        assert result.status == EnumPoolStatus.OK
        assert result.run_id == "run-ok"
        assert result.all_valid is True
        assert result.error is None
        assert len(result.ranked_candidates) == 2
        assert result.best_candidate_index in (0, 1)
        assert all(c.schema_valid for c in result.ranked_candidates)
        assert all(c.within_budget for c in result.ranked_candidates)
        assert "2/2 candidates schema-valid" in result.summary
        # Ranking is fitness-descending.
        fitness = [c.fitness_score for c in result.ranked_candidates]
        assert fitness == sorted(fitness, reverse=True)
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# OK verdict — mixed validity: a malformed (non-JSON) candidate must lose.
# Negative control: the known-bad candidate MUST score schema_valid=False.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_mixed_validity_bad_candidate_never_wins_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelCandidatePoolRequest(
                # index 0 is not JSON at all (known-bad fixture);
                # index 1 is valid and matches the schema.
                candidates=["this is not json {{{", '{"name": "good"}'],
                target_schema=_SCHEMA,
                max_loc=10,
                run_id="run-mixed",
            ),
        )
        assert result.status == EnumPoolStatus.OK
        assert result.all_valid is False
        # The valid candidate (original index 1) must be selected, never the
        # malformed one — the negative control produces the finding.
        assert result.best_candidate_index == 1
        by_index = {c.original_index: c for c in result.ranked_candidates}
        assert by_index[0].schema_valid is False
        assert by_index[1].schema_valid is True
        # schema-valid candidate ranks strictly above the invalid one.
        assert result.ranked_candidates[0].original_index == 1
        assert "1/2 candidates schema-valid" in result.summary
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# OK verdict — over-budget branch: within_budget=False penalises fitness.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_over_budget_candidate_penalised_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        # A compact valid candidate vs a valid-but-huge one (many LOC).
        compact = '{"name": "x"}'
        huge = (
            "{\n" + "".join(f'  "k{i}": {i},\n' for i in range(20)) + '  "name": "y"\n}'
        )
        result = await _drive(
            bus,
            ModelCandidatePoolRequest(
                candidates=[huge, compact],
                target_schema=_SCHEMA,
                max_loc=5,
                run_id="run-budget",
            ),
        )
        assert result.status == EnumPoolStatus.OK
        assert result.all_valid is True
        by_index = {c.original_index: c for c in result.ranked_candidates}
        # huge (index 0) exceeds the 5-LOC budget; compact (index 1) does not.
        assert by_index[0].within_budget is False
        assert by_index[1].within_budget is True
        # The compact within-budget candidate wins on fitness.
        assert result.best_candidate_index == 1
        assert by_index[1].fitness_score > by_index[0].fitness_score
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# OK verdict — schema branches: wrong root type + missing required key.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_schema_wrong_type_and_missing_required_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelCandidatePoolRequest(
                # index 0: JSON array (wrong root type — schema wants object);
                # index 1: JSON object but missing the required "name";
                # index 2: fully valid.
                candidates=["[1, 2, 3]", '{"other": "z"}', '{"name": "ok"}'],
                target_schema=_SCHEMA,
                max_loc=10,
                run_id="run-schema",
            ),
        )
        assert result.status == EnumPoolStatus.OK
        assert result.all_valid is False
        by_index = {c.original_index: c for c in result.ranked_candidates}
        assert by_index[0].schema_valid is False  # wrong root type
        assert by_index[1].schema_valid is False  # missing required key
        assert by_index[2].schema_valid is True
        assert result.best_candidate_index == 2
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# ERROR verdict — insufficient candidates (negative control).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_insufficient_candidates_error_verdict_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelCandidatePoolRequest(
                candidates=['{"name": "only-one"}'],
                target_schema=_SCHEMA,
                max_loc=10,
                min_candidates=3,
                run_id="run-error",
            ),
        )
        assert result.status == EnumPoolStatus.ERROR
        assert result.run_id == "run-error"
        assert result.ranked_candidates == []
        assert result.best_candidate_index == -1
        assert result.all_valid is False
        assert result.error is not None
        assert "at least 3" in result.error
        assert "Insufficient candidates" in result.summary
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
    command = ModelCandidatePoolRequest(
        candidates=['{"name": "a"}', "not json", '{"name": "b"}'],
        target_schema=_SCHEMA,
        max_loc=10,
        run_id="run-idem",
    )
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            result = await _drive(bus, command)
            payloads.append(result.model_dump_json())
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
