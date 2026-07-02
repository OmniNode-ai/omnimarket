# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-output COMPUTE coverage for node_context_bundle_generator_compute,
driven over the canonical in-memory bus.

OMN-13674 (cluster wave-kb-context-knowledge, archetype compute). This module
drives ``HandlerContextBundle`` end to end over ``EventBusInmemory`` (via the
``integration_event_bus`` fixture + ``LocalRuntimeBusAdapter``): a
``ModelContextBundleRequest`` lands on the declared command topic
``onex.cmd.omnimarket.context-bundle-requested.v1`` and the terminal
``ModelContextBundleResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.context-bundle-completed.v1``. No live Kafka / ``.201``.

COMPUTE DoD covered:
  * every declared output field asserted off the terminal event (``status``,
    ``bundle_id``, ``requested_level``, ``achieved_level``, ``bundle``,
    ``error``) — never a "returned without raising";
  * every declared depth branch reached: L0, L1, L2 (incl. the default when
    ``requested_level`` is omitted), L3, L4;
  * a negative control: a known-bad request (empty ``ticket_id``) MUST be
    rejected at the model boundary — the adapter records the failure and NO
    terminal event is published;
  * idempotency: identical input yields an identical ``bundle_id`` and payload.

Honest finding: the contract declares ``status`` enum ``[ok, error]`` and a
nullable ``error`` output, but ``HandlerContextBundle`` has no failure branch —
it always returns ``EnumBundleStatus.OK``. The ``error`` verdict class is
therefore unreachable through the handler; a malformed request is rejected at
the request-model boundary (asserted below) rather than yielding an ``error``
result. This is a genuine contract/impl gap, not a test omission.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_context_bundle_generator_compute.handlers.handler_context_bundle import (
    HandlerContextBundle,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_bundle_request import (
    ModelContextBundleRequest,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_bundle_result import (
    EnumBundleStatus,
    ModelContextBundleResult,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_context_bundle import (
    EnumContextLevel,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_run_context import (
    ModelRunContext,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_task_state import (
    EnumTaskPriority,
    EnumTaskStatus,
    ModelTaskState,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.context-bundle-requested.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.context-bundle-completed.v1"


def _task_state() -> ModelTaskState:
    return ModelTaskState(
        ticket_id="OMN-13674",
        title="declared-state coverage",
        status=EnumTaskStatus.IN_PROGRESS,
        assignee="jonah",
        priority=EnumTaskPriority.HIGH,
        labels=("coverage", "compute"),
        parent_ticket_id="OMN-13674-epic",
        related_ticket_ids=("OMN-13082",),
    )


def _run_context() -> ModelRunContext:
    return ModelRunContext(
        session_id="sess-1",
        agent_id="agent-1",
        timestamp="2026-07-02T00:00:00+00:00",
        worker_type="coverage",
        repo="omnimarket",
        branch="jonah/omn-13674",
        trigger_event="dispatch",
    )


def _request(level: EnumContextLevel | None) -> ModelContextBundleRequest:
    kwargs: dict[str, Any] = {
        "task_state": _task_state(),
        "run_context": _run_context(),
        "historical_summary": "prior run failed on lint",
        "prior_attempt_count": 2,
    }
    if level is not None:
        kwargs["requested_level"] = level
    return ModelContextBundleRequest(**kwargs)


async def _drive(
    bus: Any, command: ModelContextBundleRequest
) -> ModelContextBundleResult:
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerContextBundle(),
        handler_name="context-bundle",
        input_model_cls=ModelContextBundleRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-context-bundle-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=command.model_dump_json().encode("utf-8")
    )
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    assert completed[-1].topic == "onex.evt.omnimarket.context-bundle-completed.v1"
    return ModelContextBundleResult.model_validate(json.loads(completed[-1].value))


# ---------------------------------------------------------------------------
# Every declared depth branch L0..L4 over the bus.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "level",
    [
        EnumContextLevel.L0,
        EnumContextLevel.L1,
        EnumContextLevel.L2,
        EnumContextLevel.L3,
        EnumContextLevel.L4,
    ],
)
async def test_each_level_builds_matching_bundle_over_bus(
    integration_event_bus: Any, level: EnumContextLevel
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(bus, _request(level))
        assert result.status == EnumBundleStatus.OK
        assert result.error is None
        assert result.requested_level == level
        assert result.achieved_level == level
        assert result.bundle.level == level
        assert result.bundle.ticket_id == "OMN-13674"
        assert result.bundle_id  # non-empty deterministic id
        assert len(result.bundle_id) == 16
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Default level (requested_level omitted) resolves to L2.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_default_level_is_l2_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(bus, _request(None))
        assert result.status == EnumBundleStatus.OK
        assert result.requested_level == EnumContextLevel.L2
        assert result.achieved_level == EnumContextLevel.L2
        assert result.bundle.level == EnumContextLevel.L2
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Negative control — a malformed request is rejected at the model boundary and
# produces NO terminal event on the bus (the adapter records the failure).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_empty_ticket_id_rejected_no_terminal_event_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    errors: list[int] = []
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=HandlerContextBundle(),
            handler_name="context-bundle",
            input_model_cls=ModelContextBundleRequest,
            output_topic=TOPIC_COMPLETED,
            bus=bus,
            on_error=lambda: errors.append(1),
        )
        await bus.subscribe(
            TOPIC_COMMAND,
            on_message=adapter.on_message,
            group_id="omnimarket-context-bundle-neg",
        )
        # ticket_id="" violates ModelTaskState.ticket_id min_length=1.
        bad_payload = {
            "task_state": {"ticket_id": ""},
            "run_context": {"session_id": "sess-x"},
            "requested_level": "L2",
        }
        await bus.publish(
            TOPIC_COMMAND, key=None, value=json.dumps(bad_payload).encode("utf-8")
        )
        completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
        assert completed == []  # known-bad fixture produced no bundle
        assert errors == [1]  # the adapter recorded the deserialization failure
    finally:
        await bus.close()


@pytest.mark.integration
def test_empty_ticket_id_rejected_at_model_boundary() -> None:
    """The known-bad fixture MUST raise at the request-model boundary."""
    with pytest.raises(ValidationError):
        ModelTaskState(ticket_id="")


# ---------------------------------------------------------------------------
# Idempotency — identical input yields an identical bundle_id and payload.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_deterministic_identical_input_over_bus(
    integration_event_bus: Any,
) -> None:
    bus_factory = type(integration_event_bus)
    command = _request(EnumContextLevel.L4)
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
