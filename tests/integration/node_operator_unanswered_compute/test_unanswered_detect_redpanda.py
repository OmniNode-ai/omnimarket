# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same structural falsifier through the declared Redpanda test-lane overlay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

pytestmark = [pytest.mark.integration, pytest.mark.kafka]
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_operator_unanswered_compute/contract.yaml"
)


@pytest.fixture(autouse=True)
def _isolated_topic_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAFKA_TOPIC_NAMESPACE", "unanswered-" + uuid4().hex[:12])


@pytest.mark.asyncio
async def test_unanswered_detect_redpanda_overlay(
    kafka_integration_bus: EventBusKafka,
) -> None:
    raw = yaml.safe_load(CONTRACT.read_text())
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=kafka_integration_bus,
        environment="integration-test",
    )
    assert {r.contract_name: r.outcome.value for r in report.results} == {
        "node_operator_unanswered_compute": "wired"
    }
    engine.freeze()
    completed: asyncio.Queue[ModelEventEnvelope[object]] = asyncio.Queue()

    async def collect(message: ModelEventMessage) -> None:
        await completed.put(
            ModelEventEnvelope[object].model_validate_json(message.value)
        )

    await kafka_integration_bus.subscribe(
        raw["terminal_event"],
        group_id=f"unanswered-results-{uuid4()}",
        on_message=collect,
    )
    for text, expected in [("", 1), ("One line.", 0)]:
        correlation = uuid4()
        command = ModelEventEnvelope[object](
            correlation_id=correlation,
            event_type=raw["runtime_dispatch"]["command_topic"]
            .removeprefix("onex.cmd.")
            .removesuffix(".v1"),
            payload={
                "session_id": "lab-session",
                "now": datetime.now(UTC).isoformat(),
                "records": [
                    {
                        "type": "user",
                        "uuid": "operator-message",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "message": {"content": "Please answer"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "assistant-turn",
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "Bash"},
                                {"type": "text", "text": text},
                            ]
                        },
                    },
                ],
            },
        )
        await kafka_integration_bus.publish(
            raw["runtime_dispatch"]["command_topic"],
            None,
            command.model_dump_json().encode(),
            None,
        )
        event = await asyncio.wait_for(completed.get(), timeout=30)
        assert event.correlation_id == correlation
        result = event.payload
        assert isinstance(result, dict)
        assert len(result["unanswered"]) == expected
