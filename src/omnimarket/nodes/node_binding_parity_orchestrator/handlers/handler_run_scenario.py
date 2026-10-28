# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``run_scenario``: one scenario under one profile's resolved bindings."""

from __future__ import annotations

import json

from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from pydantic import JsonValue

from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelParityScenario,
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_binding_parity_orchestrator.protocols.protocol_local_bus import (
    ProtocolLocalBus,
)
from omnimarket.protocols.protocol_clock import ProtocolClock


def _text_list(fixture: dict[str, JsonValue], key: str) -> list[str]:
    value = fixture.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"scenario fixture.{key} must be a list of strings")
    return [item for item in value if isinstance(item, str)]


def _text(fixture: dict[str, JsonValue], key: str) -> str:
    value = fixture.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"scenario fixture.{key} must be a non-empty string")
    return value


async def run_stub_rows(
    *,
    profile_id: str,
    proof_eligible: bool,
    scenario: ModelParityScenario,
    bus: ProtocolLocalBus,
    clock: ProtocolClock,
) -> ModelScenarioRun:
    """The node's own scenario: publish the fixture's messages on the bus, stamp what arrives."""
    topic = _text(scenario.fixture, "topic")
    messages = _text_list(scenario.fixture, "messages")
    delivered: list[ModelEventMessage] = []

    async def on_message(message: ModelEventMessage) -> None:
        delivered.append(message)

    await bus.start()
    await bus.subscribe(topic, on_message=on_message, group_id=f"parity-{profile_id}")
    for text in messages:
        await bus.publish(topic, None, text.encode("utf-8"))
    await bus.close()
    rows = tuple(
        ModelScenarioRow(
            key=message.value.decode("utf-8"),
            content=f"{message.value.decode('utf-8')}@{clock.now().isoformat()}",
            provenance={
                "offset": str(message.offset),
                "partition": str(message.partition),
            },
        )
        for message in delivered
    )
    complete = [row.key for row in rows] == messages
    return ModelScenarioRun(
        profile_id=profile_id,
        binding=profile_id,
        proof_eligible=proof_eligible,
        verdicts=("PASS" if complete else "FAIL",),
        lines=(
            f"SEAM stub_rows published={len(messages)} delivered={len(rows)} "
            f"binding={profile_id}",
        ),
        rows=rows,
        counts={"published": len(messages), "delivered": len(rows)},
    )


async def run_node_operation(
    *,
    profile_id: str,
    proof_eligible: bool,
    scenario: ModelParityScenario,
    bus: ProtocolLocalBus,
    config: ModelRunLocalConfig,
) -> ModelScenarioRun:
    """One operation of a packaged node, run through the in-process entry on the profile's bus."""
    payload = scenario.fixture.get("request")
    if not isinstance(payload, dict):
        raise ValueError(
            "scenario fixture.request must be the operation's request mapping"
        )
    result = await run_local(scenario.node, scenario.operation, payload, config, bus)
    rows = tuple(
        ModelScenarioRow(key=name, content=json.dumps(value, sort_keys=True))
        for name, value in sorted(result.items())
    )
    return ModelScenarioRun(
        profile_id=profile_id,
        binding=profile_id,
        proof_eligible=proof_eligible,
        verdicts=("PASS",),
        lines=(
            f"SEAM node_operation {scenario.node}.{scenario.operation} "
            f"binding={profile_id}",
        ),
        rows=rows,
        counts={"rows": len(rows)},
    )
