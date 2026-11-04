# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same discovered node and injected writer over memory and Redpanda overlays."""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.handler_ask_completion_evidence import FileCompletionChecker
from omnimarket.models.model_ask_state_transition import (
    ModelAskCompletionSources,
    ModelAskStateTransitionResult,
)
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    local_serve_handler,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller, WorkLedgerAppendHost
from tests.unit.nodes.node_ask_state_compute.test_item_transition import (
    check,
    claim,
    terminal,
)
from tests.unit.nodes.node_ask_state_transition_effect.test_item_transition_effect import (
    FileRunner,
    command,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).with_name("fixtures")


async def replay_on_runtime(bus, tmp_path: Path) -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_ask_state_transition_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    discovered = tuple(
        c for c in discover_contracts().contracts if c.name == contract["name"]
    )
    assert len(discovered) == 1
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + terminal() + "\n")
    watcher = tmp_path / "watcher.json"
    watcher.write_text(
        json.dumps(
            {
                "last_tick": "2026-11-04T13:00:00Z",
                "prs": {
                    "sample#42": {
                        "facts": {
                            "state": "MERGED",
                            "merged_at": "2026-11-04T12:02:00Z",
                        }
                    }
                },
            }
        )
    )
    checker = FileCompletionChecker(
        ModelAskCompletionSources(watcher_state_path=watcher, max_age_seconds=480)
    )
    writer = local_serve_handler(
        FileRunner(ledger), ledger, "scratch-lab", ("developer",), None
    )
    host = WorkLedgerAppendHost(bus, writer, writer_principals=("developer",))
    caller = WorkLedgerAppendCaller(bus, principal="developer")
    queue = asyncio.Queue()
    correlation = uuid4()

    async def collect(message):
        raw = json.loads(message.value)
        if raw.get("correlation_id") == str(correlation):
            await queue.put(raw)

    await bus.subscribe(
        contract["terminal_event"],
        group_id=f"item-transition-results-{correlation}",
        on_message=collect,
    )
    await host.start()
    try:
        engine = MessageDispatchEngine()
        wiring = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=discovered),
            engine,
            event_bus=bus,
            environment="local",
            materialized_explicit_dependencies={
                "HandlerAskStateTransition": {
                    "appender": caller,
                    "completion_checker": checker,
                }
            },
        )
        assert [result.outcome.value for result in wiring.results] == ["wired"]
        engine.freeze()
        original = command(claim(), terminal(), checks=(check(),))
        first = None
        first_copy = None
        for _ in range(2):
            request = original.model_copy(
                update={
                    "snapshot": original.snapshot.model_copy(
                        update={"rows": tuple(ledger.read_text().splitlines())}
                    )
                }
            )
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=correlation,
                event_type=event_type_for(
                    contract["runtime_dispatch"]["command_topic"]
                ),
            )
            await bus.publish(
                contract["runtime_dispatch"]["command_topic"],
                None,
                envelope.model_dump_json().encode(),
                None,
            )
            raw = await asyncio.wait_for(queue.get(), timeout=30)
            result = ModelAskStateTransitionResult.model_validate(raw["payload"])
            assert not result.errors
            assert result.state.asks[0].state.value == "done"
            assert result.state.asks[0].owner_lane == "accountable"
            if first is None:
                first, first_copy = result.state, ledger.read_bytes()
            else:
                assert result.state == first
                assert ledger.read_bytes() == first_copy
            assert len(result.rows) == 2
    finally:
        await caller.stop()
        await host.stop()


async def test_item_transition_runtime_inmemory_overlay(tmp_path: Path) -> None:
    overlay = read_node_event_bus(FIXTURES / "overlays/inmemory.yaml")
    assert overlay is not None
    assert overlay.type == "inmemory"
    bus = bind_infra_event_bus(environment="local", group="item-transition")
    await bus.start()
    try:
        await replay_on_runtime(bus, tmp_path)
    finally:
        await bus.close()


@pytest.mark.kafka
@pytest.mark.integration
async def test_item_transition_runtime_redpanda_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    from tests.integration.customer_path.redpanda_sasl_harness import (
        resolve_broker,
        stop_redpanda,
    )

    overlay = read_node_event_bus(FIXTURES / "overlays/redpanda.yaml")
    assert overlay is not None
    assert overlay.type == "kafka"
    # The existing harness owns and tears down its scratch broker. A namespace
    # keeps this proof disjoint even when the harness adopts a declared broker.
    monkeypatch.setenv("KAFKA_TOPIC_NAMESPACE", "item-transition-" + uuid4().hex[:12])
    broker = await asyncio.to_thread(resolve_broker, boot_timeout_s=90)
    bus = EventBusKafka(
        config=ModelKafkaEventBusConfig(
            bootstrap_servers=broker.bootstrap,
            environment="local",
            security_protocol="SASL_PLAINTEXT",
            sasl_mechanism=broker.mechanism,
            sasl_plain_username=broker.username,
            sasl_plain_password=broker.password,
            auto_offset_reset="earliest",
            timeout_seconds=15,
            max_retry_attempts=1,
            enable_idempotence=False,
        )
    )
    try:
        await asyncio.wait_for(bus.start(), timeout=30)
        await replay_on_runtime(bus, tmp_path)
    finally:
        await bus.close()
        await asyncio.to_thread(stop_redpanda, broker)
