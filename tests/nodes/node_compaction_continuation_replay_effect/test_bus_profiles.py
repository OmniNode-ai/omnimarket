# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same two-boundary fixture over the in-memory or declared Redpanda test bus.

Set ONEX_CONTINUATION_TEST_PROFILE=redpanda through the lab runner for both legs.
A selected broker leg fails if unavailable; it never skips or substitutes a fake.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

import omnimarket.nodes.node_compaction_continuation_replay_effect as node_package
from omnimarket.nodes.node_compaction_continuation_replay_effect.container.container_replay import (
    create_replay_container,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_experiment import (
    HandlerReplayExperiment,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models.model_experiment import (
    ModelExperimentResult,
)
from tests.bus_lane_guard import assert_broker_in_declared_lane, resolve_test_bus_broker

from .test_experiment import ModelFixture, request

_PROFILE = os.environ.get("ONEX_CONTINUATION_TEST_PROFILE", "inmemory")
if _PROFILE not in {"inmemory", "redpanda"}:
    raise ValueError("ONEX_CONTINUATION_TEST_PROFILE must be inmemory or redpanda")
_PROFILES = ["inmemory"]
if _PROFILE == "redpanda":
    _PROFILES.append(
        pytest.param("redpanda", marks=[pytest.mark.kafka, pytest.mark.integration])
    )


@pytest.mark.parametrize("profile", _PROFILES)
async def test_two_profiles_same_two_boundaries(tmp_path: Path, profile: str) -> None:
    contract_path = Path(node_package.__file__).with_name("contract.yaml")
    contract = yaml.safe_load(contract_path.read_text())
    discovered = discover_contracts_from_paths([contract_path])
    assert not discovered.errors
    route = next(
        r
        for r in discovered.contracts[0].handler_routing.handlers
        if r.operation == "run_continuation_experiment"
    )
    container = create_replay_container(
        model=ModelFixture(True), scratch_root=tmp_path / "scratch"
    )
    node = node_package.NodeCompactionContinuationReplayEffect(container)
    assert node is not None
    handler = HandlerReplayExperiment(container)
    dispatch = _make_dispatch_callback(handler, event_model=route.event_model)
    suffix = uuid4().hex
    command = contract["runtime_dispatch"]["command_topic"] + ".it-" + suffix
    terminal = contract["terminal_event"] + ".it-" + suffix
    if profile == "inmemory":
        bus = EventBusInmemory()
    else:
        broker = resolve_test_bus_broker()
        assert_broker_in_declared_lane(broker)
        bus = EventBusKafka(
            config=ModelKafkaEventBusConfig(
                bootstrap_servers=broker,
                environment="integration-test",
                timeout_seconds=10,
                max_retry_attempts=1,
                enable_idempotence=False,
                auto_offset_reset="earliest",
                dead_letter_topic=None,
            )
        )
    done = asyncio.get_running_loop().create_future()

    async def on_command(message: ModelEventMessage) -> None:
        result = await dispatch(
            {"payload": json.loads(message.value), "correlation_id": str(uuid4())}
        )
        assert result is not None
        parsed = ModelExperimentResult.model_validate(result.output_events[0])
        await bus.publish(terminal, key=None, value=parsed.model_dump_json().encode())

    async def on_terminal(message: ModelEventMessage) -> None:
        if not done.done():
            done.set_result(ModelExperimentResult.model_validate_json(message.value))

    await bus.start()
    try:
        await bus.subscribe(
            terminal, on_message=on_terminal, group_id="continuation-result-" + suffix
        )
        await bus.subscribe(
            command, on_message=on_command, group_id="continuation-command-" + suffix
        )
        await bus.publish(
            command, key=None, value=request(tmp_path).model_dump_json().encode()
        )
        result = await asyncio.wait_for(done, timeout=45)
    finally:
        await bus.close()
    assert result.stability.unstable_rate == 0.25
    assert result.retained_cells == [("b2", "mod"), ("b2", "stock_rerun")]
    assert len(result.repeats) == 12
    assert result.repeats[0].action_score.items[0].outcome_at(2).value == "ACTED"
