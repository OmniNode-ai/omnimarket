# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Chain tests: a provision request travels command -> success/failure over the bus."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_topic_provision_plan_compute.handlers import (
    handler_topic_provision_plan as handler_module,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    plan_topic_provision,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_request import (
    ModelTopicProvisionRequest,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_topic_provision_plan_compute"
    / "contract.yaml"
)


def _topics() -> dict[str, str]:
    dispatch = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["runtime_dispatch"]
    return {
        "command": dispatch["command_topic"],
        "success": dispatch["terminal_events"]["success"],
        "failure": dispatch["terminal_events"]["failure"],
    }


class ProvisionBus:
    """Serves a provision request over the in-memory bus and records the topics carried."""

    def __init__(self) -> None:
        self.topics = _topics()
        self.bus = EventBusInmemory(
            environment="provision-chain", group="provision-chain"
        )
        self.carried: list[str] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        await self.bus.start()

        async def on_command(message: Any) -> None:
            reply = self.serve(message.value)
            await self._send(self.reply_topic(reply), reply)

        async def on_reply(message: Any) -> None:
            self._answered(message.value)

        await self.bus.subscribe(
            self.topics["command"], on_message=on_command, group_id="node"
        )
        for terminal in (self.topics["success"], self.topics["failure"]):
            await self.bus.subscribe(
                terminal, on_message=on_reply, group_id="controller"
            )

    async def _send(self, topic: str, body: bytes) -> None:
        self.carried.append(topic)
        await self.bus.publish(topic, None, body)

    async def stop(self) -> None:
        await self.bus.close()

    def serve(self, raw: bytes) -> bytes:
        request = json.loads(raw)
        try:
            req = ModelTopicProvisionRequest.model_validate(request["request"])
            plan = handler_module.plan_topic_provision(req)
        except Exception as exc:
            return json.dumps(
                {"id": request["id"], "error": f"{type(exc).__name__}: {exc}"}
            ).encode()
        return json.dumps(
            {"id": request["id"], "plan": plan.model_dump(mode="json")}
        ).encode()

    def reply_topic(self, raw: bytes) -> str:
        return (
            self.topics["failure"]
            if "error" in json.loads(raw)
            else self.topics["success"]
        )

    def _answered(self, raw: bytes) -> None:
        reply = json.loads(raw)
        future = self.waiting.pop(reply["id"], None)
        if future is not None and not future.done():
            future.set_result(reply)

    async def call(self, request: ModelTopicProvisionRequest) -> dict[str, Any]:
        call_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[call_id] = future
        body = json.dumps(
            {"id": call_id, "request": request.model_dump(mode="json")}
        ).encode()
        await self._send(self.topics["command"], body)
        return await asyncio.wait_for(future, 20)


def _valid_request() -> ModelTopicProvisionRequest:
    from omnimarket.nodes.node_topic_provision_plan_compute.models.enum_topic_provision import (
        EnumTopicProvisionMode,
    )
    from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_contract import (
        ModelTopicProvisionContract,
    )
    from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_rule import (
        ModelTopicProvisionRule,
    )

    return ModelTopicProvisionRequest(
        contracts=(
            ModelTopicProvisionContract(
                contract_name="delegation",
                runtime_profiles=("main",),
                topics=("a", "b"),
            ),
            ModelTopicProvisionContract(
                contract_name="board", runtime_profiles=("effects",), topics=("b", "c")
            ),
        ),
        profile="main",
        lane_manifest=("delegation", "board"),
        rule=ModelTopicProvisionRule(mode=EnumTopicProvisionMode.PROFILE_AND_MANIFEST),
    )


def _error_request() -> ModelTopicProvisionRequest:
    from omnimarket.nodes.node_topic_provision_plan_compute.models.enum_topic_provision import (
        EnumTopicProvisionMode,
    )
    from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_contract import (
        ModelTopicProvisionContract,
    )
    from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_rule import (
        ModelTopicProvisionRule,
    )

    return ModelTopicProvisionRequest(
        contracts=(
            ModelTopicProvisionContract(
                contract_name="delegation", runtime_profiles=("main",), topics=("a",)
            ),
        ),
        profile="main",
        lane_manifest=(),
        rule=ModelTopicProvisionRule(mode=EnumTopicProvisionMode.PROFILE_AND_MANIFEST),
    )


def _raise_plan(*_args: object) -> object:
    raise ValueError("injected fault")


async def test_golden_request_travels_command_to_success() -> None:
    topics = _topics()
    bus = ProvisionBus()
    await bus.start()
    try:
        request = _valid_request()
        reply = await bus.call(request)
        assert "error" not in reply
        expected = plan_topic_provision(request)
        assert (
            ModelTopicProvisionRequest.model_validate(
                reply["request"]
                if False
                else json.loads(json.dumps(request.model_dump(mode="json")))
            )
            is not None
        )
        assert reply["plan"] == expected.model_dump(mode="json")
        assert "c" not in expected.topics_to_provision
        assert bus.carried == [topics["command"], topics["success"]]
    finally:
        await bus.stop()


async def test_error_request_travels_command_to_failure() -> None:
    topics = _topics()
    bus = ProvisionBus()
    await bus.start()
    try:
        reply = await bus.call(_error_request())
        assert "error" in reply
        assert "TopicProvisionRuleError" in reply["error"]
        assert bus.carried == [topics["command"], topics["failure"]]
    finally:
        await bus.stop()


async def test_mutation_makes_the_golden_path_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topics = _topics()
    bus = ProvisionBus()
    await bus.start()
    try:
        monkeypatch.setattr(handler_module, "plan_topic_provision", _raise_plan)
        reply = await bus.call(_valid_request())
        assert "error" in reply
        assert bus.carried == [topics["command"], topics["failure"]]
    finally:
        await bus.stop()
