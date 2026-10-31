# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract step handlers and model-driven chain driving."""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import cast

import yaml
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.nodes.node_contract_seams_orchestrator.protocols.protocol_contract_seams_bus import (
    ProtocolContractSeamsBus,
)


class HandlerContractNodeStep:
    """In-process runner for contract node handlers."""

    def __init__(self, bus: ProtocolContractSeamsBus) -> None:
        self._bus = bus

    async def run(
        self,
        node_name: str,
        operation: str,
        payload: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        document = yaml.safe_load(
            files(f"omnimarket.nodes.{node_name}")
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
        if not isinstance(document, dict):
            raise ValueError(f"contract document for {node_name!r} must be a dict")

        handler_routing = document.get("handler_routing")
        if not isinstance(handler_routing, dict):
            raise ValueError(
                f"{node_name}: no handler is routed for operation {operation!r}"
            )
        handlers = handler_routing.get("handlers")
        if not isinstance(handlers, list):
            raise ValueError(
                f"{node_name}: no handler is routed for operation {operation!r}"
            )

        entry: dict[str, object] | None = None
        for candidate in handlers:
            if isinstance(candidate, dict) and candidate.get("operation") == operation:
                entry = candidate
                break
        if entry is None:
            raise ValueError(
                f"{node_name}: no handler is routed for operation {operation!r}"
            )

        handler_ref = entry.get("handler")
        if not isinstance(handler_ref, dict):
            raise ValueError(
                f"{node_name}: no handler is routed for operation {operation!r}"
            )
        module_name = handler_ref.get("module")
        class_name = handler_ref.get("name")
        if not isinstance(module_name, str) or not isinstance(class_name, str):
            raise ValueError(
                f"{node_name}: no handler is routed for operation {operation!r}"
            )
        module = importlib.import_module(module_name)
        handler_cls = getattr(module, class_name)
        instance = handler_cls()

        input_model_ref = entry.get("input_model") or document.get("input_model")
        if input_model_ref is None:
            handler_doc = document.get("handler")
            if isinstance(handler_doc, dict):
                input_model_ref = handler_doc.get("input_model")

        input_model: type[BaseModel]
        if isinstance(input_model_ref, dict):
            mod = input_model_ref.get("module")
            name = input_model_ref.get("name")
            if not isinstance(mod, str) or not isinstance(name, str):
                raise TypeError(f"{node_name}: input model reference is malformed")
            input_model = cast(
                type[BaseModel], getattr(importlib.import_module(mod), name)
            )
        elif isinstance(input_model_ref, str):
            if ":" in input_model_ref:
                mod, name = input_model_ref.split(":", 1)
            else:
                mod, _, name = input_model_ref.rpartition(".")
            input_model = cast(
                type[BaseModel], getattr(importlib.import_module(mod), name)
            )
        else:
            raise TypeError(f"{node_name}: input model reference is malformed")

        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise TypeError(
                f"{node_name}: input model must be a subclass of pydantic BaseModel"
            )

        request = input_model.model_validate(payload)

        runtime_dispatch = document.get("runtime_dispatch")
        command_topic: str | None = None
        if isinstance(runtime_dispatch, dict):
            ct = runtime_dispatch.get("command_topic")
            if isinstance(ct, str):
                command_topic = ct

        if command_topic is not None:
            await self._bus.publish(
                command_topic, None, json.dumps(payload, sort_keys=True).encode()
            )

        outcome = instance.handle(request)
        if inspect.isawaitable(outcome):
            outcome = await outcome

        if not isinstance(outcome, BaseModel):
            raise TypeError(
                f"{node_name}: handler for operation {operation!r} did not return a BaseModel"
            )

        terminal_event = document.get("terminal_event")
        result_topic: str | None = (
            terminal_event if isinstance(terminal_event, str) else None
        )
        if result_topic is not None:
            await self._bus.publish(
                result_topic, None, outcome.model_dump_json().encode()
            )

        result = json.loads(outcome.model_dump_json())
        if not isinstance(result, dict):
            raise TypeError(
                f"{node_name}: handler for operation {operation!r} returned a non-dict payload"
            )
        return result


class ModelDrivenContractChain(BaseModel):
    """Result of driving a model-driven contract chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_types: tuple[str, ...]
    states: tuple[str, ...]
    tail_payload: dict[str, JsonValue] | None
    delivered: bool


def _topics(contract: dict[str, object], key: str) -> tuple[str, ...]:
    """The contract's ``event_bus.<key>`` topics, sorted; absent or malformed reads empty."""
    event_bus = contract.get("event_bus")
    if not isinstance(event_bus, dict):
        return ()
    raw = cast("dict[str, object]", event_bus).get(key)
    if not isinstance(raw, list):
        return ()
    return tuple(sorted(item for item in raw if isinstance(item, str) and item))


async def drive_contract_chain(
    bus: ProtocolContractSeamsBus,
    contracts: dict[str, dict[str, object]],
    head_topic: str,
    tail_topic: str,
    correlation_id: str,
    group_prefix: str,
) -> ModelDrivenContractChain:
    events: list[str] = []
    states: list[str] = []
    tail_payload: dict[str, JsonValue] | None = None
    handled: set[tuple[str, str]] = set()
    subscriptions: list[Callable[[], Awaitable[None]]] = []

    try:
        for node_name in sorted(contracts):
            contract = contracts[node_name]
            node_publishes = _topics(contract, "publish_topics")
            for topic in _topics(contract, "subscribe_topics"):
                # Bind this node's own publish list: a closure over the loop variable
                # would publish every node's events on the last node's topics.
                async def _node_callback(
                    message: ModelEventMessage,
                    node_name: str = node_name,
                    topic: str = topic,
                    publish_topics: tuple[str, ...] = node_publishes,
                ) -> None:
                    key = (node_name, topic)
                    if key in handled:
                        return
                    handled.add(key)
                    events.append(topic)
                    states.append(node_name)
                    for pub_topic in publish_topics:
                        if pub_topic:
                            await bus.publish(
                                pub_topic,
                                None,
                                json.dumps(
                                    {
                                        "correlation_id": correlation_id,
                                        "node": node_name,
                                        "topic": pub_topic,
                                    },
                                    sort_keys=True,
                                ).encode(),
                            )

                unsub = await bus.subscribe(
                    topic,
                    on_message=_node_callback,
                    group_id=f"{group_prefix}.{node_name}",
                )
                subscriptions.append(unsub)

        async def _tail_cb(message: ModelEventMessage) -> None:
            nonlocal tail_payload
            tail_payload = cast(dict[str, JsonValue], json.loads(message.value))
            events.append(tail_topic)

        unsub_tail = await bus.subscribe(
            tail_topic, on_message=_tail_cb, group_id=f"{group_prefix}.tail"
        )
        subscriptions.append(unsub_tail)

        await bus.publish(
            head_topic, None, json.dumps({"correlation_id": correlation_id}).encode()
        )
    finally:
        for unsub in subscriptions:
            await unsub()

    return ModelDrivenContractChain(
        event_types=tuple(events),
        states=tuple(states),
        tail_payload=tail_payload,
        delivered=tail_payload is not None,
    )


__all__ = [
    "HandlerContractNodeStep",
    "ModelDrivenContractChain",
    "drive_contract_chain",
]
