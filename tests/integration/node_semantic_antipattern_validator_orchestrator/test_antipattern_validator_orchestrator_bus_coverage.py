# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-route ORCHESTRATOR coverage for
node_semantic_antipattern_validator_orchestrator, driven over the canonical
in-memory bus.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype orchestrator).

This ORCHESTRATOR declares no multi-state FSM in ``contract.yaml`` -- its declared
surface is a *single* ``handler_routing`` route (``validate_semantic_antipatterns``)
that consumes ``onex.cmd.omnimarket.antipattern-validate.v1`` and emits exactly one
``ModelAntipatternMatchCommand`` toward node_antipattern_match_effect on the declared
``publish_topic`` (``onex.cmd.omnimarket.antipattern-match-requested.v1``). There is
one internal branch: a valid ``correlation_id`` is parsed into a ``UUID`` while an
un-parseable one falls back to a fresh ``uuid4()`` for the handler output.

How it is wired
---------------
The orchestrator is dispatched through ``LocalRuntimeBusAdapter`` over the
in-memory ``integration_event_bus``: a ``ModelAntipatternValidatorRequest`` lands
on the declared command topic and the terminal ``ModelHandlerOutput`` (carrying the
emitted match command in ``events[0]``) is auto-published onto the declared
match-requested topic. Every assertion reads the emitted command off the bus, never
off internal handler state -- so the declared route + both correlation-id branches
are proven end-to-end over the bus.

No subprocess, no monkeypatch, no real Kafka: the orchestrator is pure event
forwarding, so no I/O seam needs mocking.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

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
    """Publish a validator request, return the single terminal handler-output dict.

    The terminal ``ModelHandlerOutput`` is read back off the declared match-requested
    topic as raw JSON so the emitted command (``events[0]``) can be asserted directly.
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
        output = await _drive(bus, _make_request())
        events = output["events"]
        assert len(events) == 1, "orchestrator must emit exactly one match command"
        command = events[0]
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
        output = await _drive(bus, _make_request(similarity_threshold=0.80))
        assert output["events"][0]["similarity_threshold"] == pytest.approx(0.80)
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
        output = await _drive(bus, _make_request(similarity_threshold=0.92))
        assert output["events"][0]["similarity_threshold"] == pytest.approx(0.92)
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
        output = await _drive(bus, _make_request(enforcement_mode="advisory"))
        assert output["events"][0]["enforcement_mode"] == "advisory"
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
        output = await _drive(bus, _make_request(file_content=content))
        assert output["events"][0]["file_content"] == content
    finally:
        await bus.close()


@pytest.mark.integration
async def test_valid_correlation_id_branch_over_bus(
    integration_event_bus: Any,
) -> None:
    """Valid-UUID branch: the parsed UUID is the handler-output correlation_id and
    the original string is preserved on the emitted command."""
    bus = integration_event_bus
    await bus.start()
    try:
        corr = str(uuid4())
        output = await _drive(bus, _make_request(correlation_id=corr))
        # Output correlation_id is the parsed UUID (round-trips to the same value).
        assert UUID(output["correlation_id"]) == UUID(corr)
        # Emitted command carries the caller-supplied correlation id verbatim.
        assert output["events"][0]["correlation_id"] == corr
    finally:
        await bus.close()


@pytest.mark.integration
async def test_invalid_correlation_id_falls_back_to_uuid_over_bus(
    integration_event_bus: Any,
) -> None:
    """Fallback branch: an un-parseable correlation_id yields a fresh UUID on the
    handler output while the emitted command still forwards the raw string."""
    bus = integration_event_bus
    await bus.start()
    try:
        output = await _drive(bus, _make_request(correlation_id="not-a-uuid"))
        # Output correlation_id fell back to a real uuid4 (parses cleanly).
        parsed = UUID(output["correlation_id"])
        assert str(parsed) == output["correlation_id"]
        # The emitted command still forwards the caller's raw (invalid) string.
        assert output["events"][0]["correlation_id"] == "not-a-uuid"
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
        payloads = [json.loads(e.value)["events"][0] for e in emitted]
        assert payloads[0]["file_path"] == payloads[1]["file_path"]
        assert (
            payloads[0]["similarity_threshold"] == payloads[1]["similarity_threshold"]
        )
        assert payloads[0]["correlation_id"] == payloads[1]["correlation_id"]
    finally:
        await bus.close()
