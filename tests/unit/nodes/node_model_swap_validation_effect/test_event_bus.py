# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-to-terminal proof using the real in-memory bus and runtime applier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
    _make_event_bus_callback,
)
from omnibase_infra.runtime.contract_terminal_events import (
    declared_failure_terminal_topics,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)
from omnibase_infra.utils import derive_event_type_from_topic

from omnimarket.nodes.node_model_swap_validation_effect.handlers.handler_model_swap_validation import (
    HandlerModelSwapValidation,
    load_contract,
)

from .test_handler_model_swap_validation import Validator, contract_path, request

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_model_swap_validation_effect/contract.yaml"
)


@pytest.mark.parametrize(
    ("missing_repository", "expected_topic"),
    [
        (False, "onex.evt.omnimarket.model-swap-validation-completed.v1"),
        (True, "onex.evt.omnimarket.model-swap-validation-failed.v1"),
    ],
)
async def test_command_returns_correlated_terminal_and_redelivery_replays_it(
    tmp_path: Path, missing_repository: bool, expected_topic: str
) -> None:
    config = load_contract(_CONTRACT)
    command_topic = config["event_bus"]["subscribe_topics"][0]
    success_topic = config["runtime_dispatch"]["terminal_events"]["success"]
    failed_topic = config["runtime_dispatch"]["terminal_events"]["failure"]
    provider = Validator()
    handler = HandlerModelSwapValidation(
        validator=provider, contract_path=contract_path(tmp_path)
    )
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors
    (contract,) = manifest.contracts
    assert contract.handler_routing is not None
    assert contract.event_bus is not None
    (route,) = contract.handler_routing.handlers
    callback = _make_dispatch_callback(
        cast(Any, handler),
        event_model=route.event_model,
        handler_node_kind=EnumNodeKind.EFFECT,
    )
    bus = EventBusInmemory(environment="local", group="model-swap-proof")
    await bus.start()
    records: list[tuple[str, dict[str, Any]]] = []
    try:
        applier = build_contract_result_applier(
            event_bus=cast(Any, bus),
            contract_path=_CONTRACT,
            publish_topics=contract.event_bus.publish_topics,
        )

        class Dispatch:
            async def dispatch_scoped(self, topic, envelope, *, allowed_dispatcher_ids):
                assert topic == command_topic
                assert allowed_dispatcher_ids == frozenset({"validation-fixture"})
                return await callback(envelope)

        boundary = _make_event_bus_callback(
            command_topic,
            cast(Any, Dispatch()),
            applier,
            event_bus=bus,
            allowed_dispatcher_ids={"validation-fixture"},
            failure_terminal_topics=declared_failure_terminal_topics(
                _CONTRACT,
                success_topic=success_topic,
                publishable_topics=contract.event_bus.publish_topics,
            ),
            propagate_publish_failures=True,
        )

        async def record(message: Any) -> None:
            records.append((message.topic, json.loads(message.value)))

        await bus.subscribe(
            command_topic, group_id="validation-fixture", on_message=boundary
        )
        for topic in (success_topic, failed_topic):
            await bus.subscribe(topic, group_id="terminal-fixture", on_message=record)
        req = request()
        if missing_repository:
            req = req.model_copy(update={"work_unit_repository": None})
        envelope = ModelEventEnvelope(
            payload=req.model_dump(mode="json"),
            correlation_id=req.correlation_id,
            event_type=derive_event_type_from_topic(command_topic),
        )
        for _ in range(2):
            await bus.publish(
                command_topic,
                key=str(req.correlation_id).encode(),
                value=envelope.model_dump_json().encode(),
            )
        assert len(records) == 2
        assert [topic for topic, _ in records] == [expected_topic, expected_topic]
        assert records[0][1]["payload"] == records[1][1]["payload"]
        assert records[0][1]["payload"]["correlation_id"] == str(req.correlation_id)
        assert records[0][1]["payload"]["evidence_only"] is True
        assert provider.calls == (0 if missing_repository else 1)
    finally:
        await bus.close()
