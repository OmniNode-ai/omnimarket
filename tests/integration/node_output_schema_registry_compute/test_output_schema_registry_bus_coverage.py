# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-output COMPUTE coverage for node_output_schema_registry_compute,
driven over the canonical in-memory bus.

OMN-13674 (cluster wave-D-projection-correctness-verification, archetype
compute). ``HandlerOutputSchemaRegistry`` is driven end to end over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture +
``LocalRuntimeBusAdapter``): a ``ModelSchemaRegistryRequest`` lands on the
declared command topic ``onex.cmd.omnimarket.schema-registry-requested.v1`` and
the terminal ``ModelSchemaRegistryResult`` is auto-published onto the declared
completed topic ``onex.evt.omnimarket.schema-registry-completed.v1``. No live
Kafka / ``.201``.

COMPUTE DoD covered:
  * every declared output field asserted off the terminal event
    (``status``, ``schema_key``, ``json_schema``, ``error``) — never a
    "returned without raising";
  * every declared verdict class reached: ``EnumSchemaRegistryStatus.OK`` (each
    registered schema key) and ``EnumSchemaRegistryStatus.ERROR`` (unknown key);
  * a negative control: a known-bad (unregistered) ``schema_key`` MUST resolve
    to ``status=error`` with ``json_schema is None`` and a populated ``error``
    naming the known keys — it never fabricates a schema;
  * idempotency: identical input yields an identical terminal event.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_output_schema_registry_compute.handlers.handler_output_schema_registry import (
    HandlerOutputSchemaRegistry,
    known_schema_keys,
)
from omnimarket.nodes.node_output_schema_registry_compute.models.model_schema_registry_request import (
    ModelSchemaRegistryRequest,
)
from omnimarket.nodes.node_output_schema_registry_compute.models.model_schema_registry_result import (
    EnumSchemaRegistryStatus,
    ModelSchemaRegistryResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.schema-registry-requested.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.schema-registry-completed.v1"


async def _drive(
    bus: Any, command: ModelSchemaRegistryRequest, *, group: str
) -> ModelSchemaRegistryResult:
    """Publish the command onto the declared topic and read the terminal event
    back off the declared completed topic — the whole flow transits the bus."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerOutputSchemaRegistry(),
        handler_name="output-schema-registry",
        input_model_cls=ModelSchemaRegistryRequest,
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
    assert completed[-1].topic == "onex.evt.omnimarket.schema-registry-completed.v1"
    return ModelSchemaRegistryResult.model_validate(json.loads(completed[-1].value))


# ---------------------------------------------------------------------------
# OK verdict — every registered schema key resolves to a JSON schema.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("schema_key", known_schema_keys())
async def test_registered_key_resolves_ok_over_bus(
    integration_event_bus: Any, schema_key: str
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelSchemaRegistryRequest(schema_key=schema_key, run_id="run-ok"),
            group=f"schema-ok-{schema_key}",
        )
        assert result.status == EnumSchemaRegistryStatus.OK
        assert result.schema_key == schema_key
        # json_schema is a non-empty Pydantic model_json_schema() dict.
        assert result.json_schema is not None
        assert isinstance(result.json_schema, dict)
        assert result.json_schema != {}
        assert "type" in result.json_schema or "properties" in result.json_schema
        # error output field is None on the success path.
        assert result.error is None
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# ERROR verdict — unknown key (negative control). Must NOT fabricate a schema.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_key_error_verdict_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelSchemaRegistryRequest(
                schema_key="definitely_not_registered", run_id="run-err"
            ),
            group="schema-err",
        )
        assert result.status == EnumSchemaRegistryStatus.ERROR
        assert result.schema_key == "definitely_not_registered"
        # Negative control: the unknown key produces the finding — no schema.
        assert result.json_schema is None
        assert result.error is not None
        assert "definitely_not_registered" in result.error
        # The error names the known keys so callers can self-correct.
        for known in known_schema_keys():
            assert known in result.error
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
    command = ModelSchemaRegistryRequest(schema_key="review_output", run_id="run-idem")
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            result = await _drive(bus, command, group="schema-idem")
            payloads.append(result.model_dump_json())
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
