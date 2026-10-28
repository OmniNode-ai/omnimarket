# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``run_local``: run one operation of any packaged node on an in-memory bus.

``onex run`` takes a fixed choice of two named workflows and ``onex run-node`` dispatches over a
broker, so neither runs an arbitrary node with no infrastructure. This entry does: it reads the
node's own contract, publishes the request as a command on the bus, lets a consumer on that bus
call the node's handler for the operation, and reads the result back from the result topic.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from collections.abc import Callable
from importlib.resources import files

import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from pydantic import BaseModel, JsonValue

from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_binding_parity_orchestrator.protocols.protocol_local_bus import (
    ProtocolLocalBus,
)


def build_local_bus() -> EventBusInmemory:
    """The container's default bus: core's in-memory event bus."""
    return EventBusInmemory(environment="in-process-entry", group="run-in-process")


def _split(path: str) -> tuple[str, str]:
    module, _, name = path.partition(":")
    if not name:
        module, _, name = path.rpartition(".")
    return module, name


def resolve_operation(
    node_name: str, operation: str
) -> tuple[type[BaseModel], Callable[[BaseModel], object]]:
    """The input model and a bound ``handle`` for one operation of a packaged node's contract."""
    document = yaml.safe_load(
        files(f"omnimarket.nodes.{node_name}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    for entry in document["handler_routing"]["handlers"]:
        if entry["operation"] != operation:
            continue
        handler_ref = entry["handler"]
        handler_module = importlib.import_module(handler_ref["module"])
        handler = getattr(handler_module, handler_ref["name"])()
        input_ref = entry.get("input_model") or document["input_model"]
        if isinstance(input_ref, dict):
            module_name, class_name = input_ref["module"], input_ref["name"]
        else:
            module_name, class_name = _split(input_ref)
        input_model = getattr(importlib.import_module(module_name), class_name)
        return input_model, handler.handle
    raise ValueError(f"{node_name}: no handler is routed for operation {operation!r}")


async def run_local(
    node_name: str,
    operation: str,
    payload: dict[str, JsonValue],
    config: ModelRunLocalConfig,
    bus: ProtocolLocalBus | None = None,
) -> dict[str, JsonValue]:
    """Run ``operation`` of ``node_name`` over ``bus`` (the in-memory bus when none is given)."""
    input_model, handle = resolve_operation(node_name, operation)
    local_bus: ProtocolLocalBus = bus if bus is not None else build_local_bus()
    result_ready = asyncio.Event()
    results: list[dict[str, JsonValue]] = []
    failures: list[Exception] = []

    async def consume_command(message: ModelEventMessage) -> None:
        # A bus counts and logs a consumer's exception; the in-process entry surfaces it too.
        try:
            request = input_model.model_validate(json.loads(message.value))
            outcome = handle(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if not isinstance(outcome, BaseModel):
                raise TypeError(
                    f"{node_name}.{operation} returned {type(outcome).__name__}"
                )
            await local_bus.publish(
                config.result_topic, None, outcome.model_dump_json().encode("utf-8")
            )
        except Exception as raised:
            failures.append(raised)
            result_ready.set()
            raise

    async def consume_result(message: ModelEventMessage) -> None:
        results.append(json.loads(message.value))
        result_ready.set()

    await local_bus.start()
    try:
        await local_bus.subscribe(
            config.command_topic,
            on_message=consume_command,
            group_id=config.consumer_group,
        )
        await local_bus.subscribe(
            config.result_topic,
            on_message=consume_result,
            group_id=config.consumer_group,
        )
        await local_bus.publish(
            config.command_topic, None, json.dumps(payload).encode("utf-8")
        )
        await asyncio.wait_for(result_ready.wait(), timeout=config.timeout_seconds)
    finally:
        await local_bus.close()
    if failures:
        raise failures[0]
    return results[0]
