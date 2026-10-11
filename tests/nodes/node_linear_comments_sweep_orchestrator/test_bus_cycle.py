# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One full sweep over the canonical in-memory bus, no injected gateway, the real decision node.

The start command is published on the contract's declared topic and both contracts
(this node and node_linear_comments_decision_compute) are wired through the
runtime's auto-wiring from their paths. Every model call rides the declared
delegation request/reply topics and every decision rides the decision node's own
command and terminal topics, so the cross-node contract is exercised end to end.
Two stand-ins: the delegation consumer, which answers each request with a recorded
result, and the infra request/reply wiring, whose Kafka consumer is replaced by an
in-memory correlation matcher on the same bus (the real wiring needs a broker).
"""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from pathlib import Path
from typing import cast
from uuid import UUID

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

from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    OVERLAY_ENV,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
    ModelLinearCommentsSweepRequest,
    ModelLinearCommentsSweepResult,
    ModelSweepPhaseRequest,
)

from .helpers import (
    ITEMS,
    NODE,
    OVERLAY_FILE,
    SELECTED,
    FixtureGateway,
    collected_content,
    draft_for,
    request,
)

CORRELATION = UUID("12345678-1234-5678-1234-567812345678")
PACKAGE = f"omnimarket.nodes.{NODE}"
DECISION_PACKAGE = "omnimarket.nodes.node_linear_comments_decision_compute"
START = "onex.cmd.omnimarket.linear-comments-sweep-start.v1"
COMPLETED = "onex.evt.omnimarket.linear-comments-sweep-completed.v1"
FAILED = "onex.evt.omnimarket.linear-comments-sweep-failed.v1"


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


async def drive(
    broken: str | None,
) -> tuple[list[ModelLinearCommentsSweepResult], list[bytes], list[str], list[str]]:
    """Publish one start command; return completed terminals, failed terminals, models asked, decisions."""
    contract_file = files(PACKAGE).joinpath("contract.yaml")
    contract = yaml.safe_load(contract_file.read_text())
    decision_file = files(DECISION_PACKAGE).joinpath("contract.yaml")
    delegation = next(
        i
        for i in contract["event_bus"]["request_response"]["instances"]
        if i["name"] == "sweep_phase"
    )
    bus = EventBusInmemory(environment="dev", group="sweep-cycle")
    await bus.start()
    asked: list[str] = []
    decisions: list[str] = []
    completed: list[ModelLinearCommentsSweepResult] = []
    failed: list[bytes] = []
    settled = asyncio.Event()

    async def delegation_consumer(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
            message.value
        )
        kind, content, model = _answer(envelope.payload.prompt)
        asked.append(kind)
        reply: dict[str, object] = {
            "correlation_id": str(envelope.correlation_id),
            "tenant_id": envelope.payload.tenant_id,
            "content": content,
            "model_used": model,
            "total_tokens": 30,
            "latency_ms": 400,
        }
        if kind == broken:
            reply = {**reply, "content": "", "failure_reason": "stand-in refused"}
        await bus.publish(
            delegation["reply_topics"]["completed"],
            str(envelope.correlation_id).encode(),
            json.dumps(reply).encode(),
        )

    async def on_decision(message: ModelEventMessage) -> None:
        decisions.append(json.loads(message.value)["payload"]["kind"])

    async def on_completed(message: ModelEventMessage) -> None:
        completed.append(
            ModelEventEnvelope[ModelLinearCommentsSweepResult]
            .model_validate_json(message.value)
            .payload
        )
        settled.set()

    async def on_failed(message: ModelEventMessage) -> None:
        failed.append(message.value)
        settled.set()

    stops = [
        await bus.subscribe(
            delegation["request_topic"],
            group_id="sweep-cycle-delegation",
            on_message=delegation_consumer,
        ),
        await bus.subscribe(
            "onex.cmd.omnimarket.linear-comments-decision-requested.v1",
            group_id="sweep-cycle-decision-spy",
            on_message=on_decision,
        ),
        await bus.subscribe(
            COMPLETED, group_id="sweep-cycle-ok", on_message=on_completed
        ),
        await bus.subscribe(FAILED, group_id="sweep-cycle-err", on_message=on_failed),
    ]
    manifest = discover_contracts_from_paths(
        [Path(str(contract_file)), Path(str(decision_file))]
    )
    assert manifest.total_discovered == 2
    assert not manifest.errors
    engine = MessageDispatchEngine()
    declared = {
        c["name"]: c for c in (contract, yaml.safe_load(decision_file.read_text()))
    }
    appliers = {
        c.name: DispatchResultApplier(
            cast(ProtocolEventBusLike, bus),
            output_topic=declared[c.name]["terminal_event"],
            allowed_output_topics=declared[c.name]["event_bus"]["publish_topics"],
        )
        for c in manifest.contracts
    }
    report = await wire_from_manifest(
        manifest,
        engine,
        bus,
        subscribe_immediately=False,
        result_appliers_by_contract=appliers,
        materialized_explicit_dependencies={
            "HandlerLinearCommentsSweep": {"event_bus": bus}
        },
    )
    engine.freeze()
    attached = await subscribe_wired_contract_topics(
        manifest, report, engine, bus, result_appliers_by_contract=appliers
    )
    assert START in attached[NODE]
    command = request()
    envelope = ModelEventEnvelope[ModelLinearCommentsSweepRequest](
        payload=command,
        correlation_id=command.correlation_id,
        tenant_id=command.tenant_id,
        event_type=START,
        envelope_timestamp=command.emitted_at,
    )
    await bus.publish(
        START, str(command.correlation_id).encode(), envelope.model_dump_json().encode()
    )
    await asyncio.wait_for(settled.wait(), timeout=60)
    for stop in stops:
        await stop()
    await bus.close()
    return completed, failed, asked, decisions


def _answer(prompt: str) -> tuple[str, str, str]:
    """The recorded stand-in knows a call by its brief, as a consumer knows it by its prompt."""
    if prompt.startswith("COLLECT"):
        return "collect", collected_content(), "sonnet"
    if prompt.startswith("WRITE"):
        return "write", FixtureGateway.default(_write_phase()).content, "sonnet"
    if prompt.startswith("VERIFY"):
        comment_id = prompt.split()[2]
        item = next(i for i in ITEMS if i["comment_id"] == comment_id)
        verdict = {"accepted": True, "draft": draft_for(item)}
        return "verify", json.dumps(verdict), "opus"
    item = next(i for i in ITEMS if str(i["comment_id"]) in prompt)
    return "delegate", json.dumps(draft_for(item)), "qwen"


def _write_phase() -> ModelSweepPhaseRequest:
    return ModelSweepPhaseRequest(
        label="write:linear-file",
        phase="Finalize",
        task_type="agent_delegation",
        prompt="",
    )


@pytest.fixture
def bus_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    monkeypatch.setenv("ONEX_ENVIRONMENT", "dev")
    monkeypatch.setattr(
        "omnibase_infra.runtime.request_response_wiring.RequestResponseWiring",
        InMemoryRequestResponse,
    )


@pytest.mark.usefixtures("bus_environment")
def test_one_sweep_cycle_over_the_bus() -> None:
    completed, failed, asked, decisions = asyncio.run(drive(None))
    assert failed == []
    assert len(completed) == 1
    terminal = completed[0]
    assert terminal.correlation_id == CORRELATION
    assert terminal.needing == len(SELECTED)
    assert terminal.accepted == len(SELECTED)
    assert terminal.held == 0
    assert terminal.degraded is False
    assert terminal.linear_file == "drafts/linear-comment-replies-2026-09-14T104031Z.md"
    assert asked[0] == "collect"
    assert asked[-1] == "write"
    assert sorted(asked[1:-1]) == ["delegate"] * len(SELECTED) + ["verify"] * len(
        SELECTED
    )
    # Every decision of the sweep crossed the bus to the decision node, which answered each one.
    assert decisions[0] == "decide_items"
    assert decisions[-1] == "tally"
    assert decisions.count("build_prompt") == len(SELECTED)
    assert decisions.count("check_draft") == len(SELECTED)
    assert decisions.count("metrics_row") == len(SELECTED)


@pytest.mark.usefixtures("bus_environment")
def test_a_refused_delegation_over_the_bus_is_held_and_the_window_degrades() -> None:
    completed, failed, _asked, _decisions = asyncio.run(drive("delegate"))
    assert failed == []
    assert len(completed) == 1
    terminal = completed[0]
    assert terminal.accepted == 0
    assert terminal.held == len(SELECTED)
    assert terminal.degraded is True
    assert terminal.held_by_class["transport"] == len(SELECTED)


@pytest.mark.usefixtures("bus_environment")
def test_a_refused_collector_over_the_bus_terminates_as_failed() -> None:
    completed, failed, asked, _decisions = asyncio.run(drive("collect"))
    assert completed == []
    assert len(failed) == 1
    assert b"collect phase failed" in failed[0]
    assert asked == ["collect"]
