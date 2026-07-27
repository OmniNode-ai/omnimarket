# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-route ORCHESTRATOR coverage for
node_semantic_antipattern_validator_orchestrator, driven over the canonical
in-memory bus.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype orchestrator).
OMN-14242: HandlerAntipatternValidatorOrchestrator.handle() returns the typed
``ModelAntipatternMatchCommand`` directly (thin canonical shape -- no
``ModelHandlerOutput`` envelope, no coercion in the handler).

This ORCHESTRATOR declares no multi-state FSM in ``contract.yaml`` -- its declared
surface is a *single* ``handler_routing`` route (``validate_semantic_antipatterns``)
that consumes ``onex.cmd.omnimarket.antipattern-validate.v1`` and emits exactly one
``ModelAntipatternMatchCommand`` toward node_antipattern_match_effect on the declared
``publish_topic`` (``onex.cmd.omnimarket.antipattern-match-requested.v1``).
``correlation_id`` is forwarded verbatim from the request onto the emitted command --
no UUID parsing/fallback happens in the handler (that logic previously existed only
to populate the now-deleted ``ModelHandlerOutput`` envelope's own correlation_id).

How it is wired
---------------
The orchestrator is dispatched through ``LocalRuntimeBusAdapter`` over the
in-memory ``integration_event_bus``: a ``ModelAntipatternValidatorRequest`` lands
on the declared command topic and the handler's returned
``ModelAntipatternMatchCommand`` is published directly (as its own JSON) onto the
declared match-requested topic -- the runtime, not the handler, does the
publication. Every assertion reads the emitted command off the bus, never off
internal handler state.

No subprocess, no monkeypatch, no real Kafka: the orchestrator is pure event
forwarding, so no I/O seam needs mocking.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.handlers.handler_antipattern_validator_orchestrator import (
    HandlerAntipatternValidatorOrchestrator,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_validator_request import (
    ModelAntipatternValidatorRequest,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Declared wire strings (node contract.yaml -> event_bus). Pinned against the
# contract in test_antipattern_validator_orchestrator_state_coverage.py.
TOPIC_VALIDATE = "onex.cmd.omnimarket.antipattern-validate.v1"
TOPIC_MATCH_REQUESTED = "onex.cmd.omnimarket.antipattern-match-requested.v1"


async def _drive(
    bus: Any,
    request: ModelAntipatternValidatorRequest,
) -> dict[str, Any]:
    """Publish a validator request, return the emitted match-command dict.

    The handler's returned ``ModelAntipatternMatchCommand`` is read back off the
    declared match-requested topic as raw JSON.
    """
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerAntipatternValidatorOrchestrator(),
        handler_name="antipattern-validator-orchestrator",
        input_model_cls=ModelAntipatternValidatorRequest,
        output_topic=TOPIC_MATCH_REQUESTED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_VALIDATE,
        on_message=adapter.on_message,
        group_id="omnimarket-antipattern-validator-orchestrator-test",
    )
    await bus.publish(
        TOPIC_VALIDATE,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )

    emitted = await bus.get_event_history(topic=TOPIC_MATCH_REQUESTED)
    assert len(emitted) == 1, f"expected exactly one terminal output, got {emitted}"
    output: dict[str, Any] = json.loads(emitted[-1].value)
    return output


def _make_request(
    *,
    file_path: str = "src/omnimarket/nodes/node_x/handlers/handler_x.py",
    file_content: str = "class Foo:\n    def a(self): ...\n    def b(self): ...\n",
    enforcement_mode: str = "blocking",
    similarity_threshold: float = 0.80,
    correlation_id: str | None = None,
) -> ModelAntipatternValidatorRequest:
    return ModelAntipatternValidatorRequest(
        file_path=file_path,
        file_content=file_content,
        enforcement_mode=enforcement_mode,
        similarity_threshold=similarity_threshold,
        correlation_id=correlation_id if correlation_id is not None else str(uuid4()),
    )


@pytest.mark.integration
async def test_route_fires_and_emits_single_match_command_over_bus(
    integration_event_bus: Any,
) -> None:
    """The declared validate route emits exactly one match command toward the effect."""
    bus = integration_event_bus
    await bus.start()
    try:
        command = await _drive(bus, _make_request())
        # The emitted command targets the antipattern match effect boundary.
        assert (
            command["file_path"] == "src/omnimarket/nodes/node_x/handlers/handler_x.py"
        )
        assert command["enforcement_mode"] == "blocking"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_default_threshold_propagated_over_bus(
    integration_event_bus: Any,
) -> None:
    """Default similarity_threshold (0.80) flows into the emitted command payload."""
    bus = integration_event_bus
    await bus.start()
    try:
        command = await _drive(bus, _make_request(similarity_threshold=0.80))
        assert command["similarity_threshold"] == pytest.approx(0.80)
    finally:
        await bus.close()


@pytest.mark.integration
async def test_custom_threshold_propagated_over_bus(
    integration_event_bus: Any,
) -> None:
    """A non-default threshold is forwarded verbatim to the match command."""
    bus = integration_event_bus
    await bus.start()
    try:
        command = await _drive(bus, _make_request(similarity_threshold=0.92))
        assert command["similarity_threshold"] == pytest.approx(0.92)
    finally:
        await bus.close()


@pytest.mark.integration
async def test_advisory_enforcement_mode_propagated_over_bus(
    integration_event_bus: Any,
) -> None:
    """The advisory enforcement branch is forwarded to the effect unchanged."""
    bus = integration_event_bus
    await bus.start()
    try:
        command = await _drive(bus, _make_request(enforcement_mode="advisory"))
        assert command["enforcement_mode"] == "advisory"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_file_content_propagated_over_bus(
    integration_event_bus: Any,
) -> None:
    """The file_content to scan is carried through to the effect command."""
    bus = integration_event_bus
    await bus.start()
    try:
        content = "def god_function():\n    return 1\n"
        command = await _drive(bus, _make_request(file_content=content))
        assert command["file_content"] == content
    finally:
        await bus.close()


@pytest.mark.integration
async def test_valid_correlation_id_forwarded_verbatim_over_bus(
    integration_event_bus: Any,
) -> None:
    """A valid-UUID correlation_id is forwarded verbatim onto the emitted command."""
    bus = integration_event_bus
    await bus.start()
    try:
        corr = str(uuid4())
        command = await _drive(bus, _make_request(correlation_id=corr))
        assert command["correlation_id"] == corr
    finally:
        await bus.close()


@pytest.mark.integration
async def test_invalid_correlation_id_forwarded_verbatim_over_bus(
    integration_event_bus: Any,
) -> None:
    """A non-UUID correlation_id is forwarded verbatim too (OMN-14242: the thin
    handler no longer parses/falls back -- that logic only ever fed the deleted
    ModelHandlerOutput envelope's own correlation_id, not the emitted command)."""
    bus = integration_event_bus
    await bus.start()
    try:
        command = await _drive(bus, _make_request(correlation_id="not-a-uuid"))
        assert command["correlation_id"] == "not-a-uuid"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_repeated_requests_each_emit_one_command_idempotent_shape(
    integration_event_bus: Any,
) -> None:
    """Two identical requests each emit exactly one command with identical payload
    (the forwarding is deterministic in the fields it propagates)."""
    bus = integration_event_bus
    await bus.start()
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=HandlerAntipatternValidatorOrchestrator(),
            handler_name="antipattern-validator-orchestrator",
            input_model_cls=ModelAntipatternValidatorRequest,
            output_topic=TOPIC_MATCH_REQUESTED,
            bus=bus,
        )
        await bus.subscribe(
            TOPIC_VALIDATE,
            on_message=adapter.on_message,
            group_id="omnimarket-antipattern-validator-orchestrator-test",
        )
        req = _make_request(correlation_id=str(uuid4()), similarity_threshold=0.83)
        for _ in range(2):
            await bus.publish(
                TOPIC_VALIDATE,
                key=None,
                value=req.model_dump_json().encode("utf-8"),
            )

        emitted = await bus.get_event_history(topic=TOPIC_MATCH_REQUESTED)
        assert len(emitted) == 2
        payloads = [json.loads(e.value) for e in emitted]
        assert payloads[0]["file_path"] == payloads[1]["file_path"]
        assert (
            payloads[0]["similarity_threshold"] == payloads[1]["similarity_threshold"]
        )
        assert payloads[0]["correlation_id"] == payloads[1]["correlation_id"]
    finally:
        await bus.close()
