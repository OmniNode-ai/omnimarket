# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One full friction sweep cycle over the canonical in-memory bus, no injected gateway.

The start command is published on the contract's declared topic, the contract is
wired through the runtime's auto-wiring from its path, the handler builds its own
bus phase gateway, every phase rides the declared delegation request/reply topics,
and the terminal lands on the declared completed topic. Two stand-ins: the
delegation consumer, which answers each request with a recorded phase result, and
the infra request/reply wiring, whose Kafka consumer is replaced by an in-memory
correlation matcher on the same bus (the real wiring needs a broker).
"""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.contracts.subcontracts import ModelRequestResponseConfig
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring import (
    subscribe_wired_contract_topics,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.service_dispatch_result_applier import DispatchResultApplier

from omnimarket.nodes.node_morning_friction_sweep_orchestrator.handlers.handler_morning_friction_sweep import (
    OVERLAY_ENV,
)
from omnimarket.nodes.node_morning_friction_sweep_orchestrator.models import (
    ModelMorningFrictionSweepRequest,
    ModelMorningFrictionSweepResult,
)

from .helpers import NODE, OVERLAY_FILE, request, stub_result

CORRELATION = UUID("12345678-1234-5678-1234-567812345678")

PACKAGE = f"omnimarket.nodes.{NODE}"


class InMemoryRequestResponse:
    """Correlation matching on the in-memory bus, in place of the Kafka consumer."""

    def __init__(self, event_bus: EventBusInmemory, **_: object) -> None:
        self.bus = event_bus
        self.pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.instances: dict[str, str] = {}

    async def wire_request_response(self, config: ModelRequestResponseConfig) -> None:
        for instance in config.instances:
            self.instances[instance.name] = instance.request_topic
            for reply in (
                instance.reply_topics.completed,
                instance.reply_topics.failed,
            ):
                await self.bus.subscribe(
                    reply,
                    group_id=f"rr-{instance.name}-{reply}",
                    on_message=self._on_reply,
                )

    async def _on_reply(self, message: ModelEventMessage) -> None:
        body = json.loads(message.value)
        pending = self.pending.pop(str(body["correlation_id"]), None)
        if pending is not None:
            pending.set_result(body)

    async def send_request(
        self,
        instance_name: str,
        payload: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        correlation = str(payload["correlation_id"])
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[correlation] = future
        await self.bus.publish(
            self.instances[instance_name],
            correlation.encode(),
            json.dumps(payload).encode(),
        )
        return await asyncio.wait_for(future, timeout=timeout_seconds)


LABELS = {
    uuid5(CORRELATION, label): label
    for label in (
        "friction-precheck",
        "friction-scan",
        "friction-source-linear",
        "friction-source-checkpoints",
        "friction-source-ci",
        "friction-source-guards",
        "friction-synthesize",
        "friction-adjudicate",
        "friction-report",
    )
}


COMPLETED = "onex.evt.omnimarket.morning-friction-sweep-completed.v1"
FAILED = "onex.evt.omnimarket.morning-friction-sweep-failed.v1"
START = "onex.cmd.omnimarket.morning-friction-sweep-start.v1"


async def drive(
    bad_phase: str | None,
) -> tuple[list[ModelMorningFrictionSweepResult], list[bytes], list[str]]:
    """Publish one start command; return completed terminals, failed terminals, phases asked."""
    contract_file = files(PACKAGE).joinpath("contract.yaml")
    contract = yaml.safe_load(contract_file.read_text())
    start = contract["event_bus"]["subscribe_topics"][0]
    assert start == START
    instance = contract["event_bus"]["request_response"]["instances"][0]
    bus = EventBusInmemory(environment="dev", group="friction-cycle")
    await bus.start()
    asked: list[str] = []
    completed: list[ModelMorningFrictionSweepResult] = []
    failed: list[bytes] = []
    settled = asyncio.Event()

    async def delegation_consumer(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
            message.value
        )
        phase = json.loads(envelope.payload.context_pack or "{}").get("phase", "")
        asked.append(phase)
        label = LABELS[envelope.payload.correlation_id]
        result = stub_result(label)
        if phase == bad_phase and result is not None:
            result["delegation"] = {"delegated": 1, "runs": [], "reason": ""}
        reply = {
            "correlation_id": str(envelope.correlation_id),
            "tenant_id": envelope.payload.tenant_id,
            "content": json.dumps(result),
        }
        await bus.publish(
            instance["reply_topics"]["completed"],
            str(envelope.correlation_id).encode(),
            json.dumps(reply).encode(),
        )

    async def on_completed(message: ModelEventMessage) -> None:
        completed.append(
            ModelEventEnvelope[ModelMorningFrictionSweepResult]
            .model_validate_json(message.value)
            .payload
        )
        settled.set()

    async def on_failed(message: ModelEventMessage) -> None:
        failed.append(message.value)
        settled.set()

    stops = [
        await bus.subscribe(
            instance["request_topic"],
            group_id="friction-cycle-delegation",
            on_message=delegation_consumer,
        ),
        await bus.subscribe(
            COMPLETED, group_id="friction-cycle-ok", on_message=on_completed
        ),
        await bus.subscribe(
            FAILED, group_id="friction-cycle-err", on_message=on_failed
        ),
    ]
    manifest = discover_contracts_from_paths([Path(str(contract_file))])
    assert manifest.total_discovered == 1
    assert not manifest.errors
    engine = MessageDispatchEngine()
    appliers = {
        manifest.contracts[0].name: DispatchResultApplier(
            cast(ProtocolEventBusLike, bus),
            output_topic=contract["terminal_event"],
            allowed_output_topics=contract["event_bus"]["publish_topics"],
        )
    }
    report = await wire_from_manifest(
        manifest,
        engine,
        bus,
        subscribe_immediately=False,
        result_appliers_by_contract=appliers,
        materialized_explicit_dependencies={
            "HandlerMorningFrictionSweep": {"event_bus": bus}
        },
    )
    engine.freeze()
    attached = await subscribe_wired_contract_topics(
        manifest, report, engine, bus, result_appliers_by_contract=appliers
    )
    assert start in attached[manifest.contracts[0].name]
    command = request({"force": True})
    envelope = ModelEventEnvelope[ModelMorningFrictionSweepRequest](
        payload=command,
        correlation_id=command.correlation_id,
        tenant_id=command.tenant_id,
        event_type=start,
        envelope_timestamp=command.emitted_at,
    )
    await bus.publish(
        start, str(command.correlation_id).encode(), envelope.model_dump_json().encode()
    )
    await asyncio.wait_for(settled.wait(), timeout=30)
    for stop in stops:
        await stop()
    await bus.close()
    return completed, failed, asked


@pytest.fixture
def bus_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    monkeypatch.setenv("ONEX_ENVIRONMENT", "dev")
    monkeypatch.setattr(
        "omnibase_infra.runtime.request_response_wiring.RequestResponseWiring",
        InMemoryRequestResponse,
    )


@pytest.mark.usefixtures("bus_environment")
def test_one_friction_sweep_cycle_over_the_bus() -> None:
    completed, failed, asked = asyncio.run(drive(None))
    assert failed == []
    assert len(completed) == 1
    terminal = completed[0]
    assert terminal.correlation_id == CORRELATION
    assert terminal.short_circuited is None
    assert terminal.agents == 8
    assert terminal.report == "/tmp/report.md"
    # Force skips the precheck; five concurrent sources, then the three serial phases.
    assert sorted(asked[:5]) == ["Scan"] * 5
    assert asked[5:] == ["Synthesize", "Adjudicate", "Report"]


@pytest.mark.usefixtures("bus_environment")
@pytest.mark.parametrize("bad_phase", ["Synthesize", "Adjudicate", "Report"])
def test_invalid_delegation_evidence_over_the_bus_terminates_as_failed(
    bad_phase: str,
) -> None:
    completed, failed, _asked = asyncio.run(drive(bad_phase))
    assert completed == []
    assert len(failed) == 1
    assert b"delegation cell refused" in failed[0]
