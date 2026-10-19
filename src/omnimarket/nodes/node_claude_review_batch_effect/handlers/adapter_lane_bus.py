# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve one batch command on a lab lane bus and read every event back (OMN-20910).

The bus is opened exactly as ``onex delegate --lane`` opens it. The command is
an event envelope on the contract's command topic; the handler runs when the
command is delivered; each event it publishes is an envelope on its topic; and a
subscription on every published topic, each in its own consumer group, reads
them back off the broker, so what is printed is what the bus delivered.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.handler_claude_review_batch import (
    HandlerClaudeReviewBatch,
)
from omnimarket.nodes.node_claude_review_batch_effect.models import (
    ModelClaudeReviewBatchRequest,
)

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"


class BusEventSink:
    """Publish handler events from the handler's worker thread onto the bus.

    The handler runs under ``asyncio.to_thread``, so the loop stays free to run
    each publish while the worker waits on it. A publish from the loop's own
    thread would wait on itself, so it is refused rather than left to hang.
    """

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        loop: asyncio.AbstractEventLoop,
        correlation: uuid.UUID,
    ) -> None:
        self._bus, self._loop, self._correlation = bus, loop, correlation

    def publish(self, topic: str, event: BaseModel) -> None:
        try:
            on_loop_thread = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop_thread = False
        if on_loop_thread:
            raise RuntimeError(
                "BusEventSink.publish was called on the event loop's thread; "
                "run the handler with asyncio.to_thread"
            )
        value = envelope_bytes_for(
            topic, event.model_dump(mode="json"), self._correlation
        )
        asyncio.run_coroutine_threadsafe(
            self._bus.publish(topic, str(self._correlation).encode(), value), self._loop
        ).result(timeout=60)


async def serve_and_read_back(
    bus: ProtocolLabRunBus,
    request: ModelClaudeReviewBatchRequest,
    build_handler: Callable[[BusEventSink], HandlerClaudeReviewBatch],
    *,
    wait_seconds: float = 600.0,
) -> list[dict[str, Any]]:
    """Publish ``request`` as the command, serve it, return the events read back, in order."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    command_topic = contract["runtime_dispatch"]["command_topic"]
    event_topics = list(contract["event_bus"]["publish_topics"])
    run = uuid.uuid4().hex[:12]
    correlation = uuid.uuid4()
    loop = asyncio.get_running_loop()

    seen: list[dict[str, Any]] = []
    done = asyncio.Event()
    commands: asyncio.Queue[bytes] = asyncio.Queue()
    terminal = event_topics[-1]
    unsubscribes: list[Callable[[], Awaitable[None]]] = []

    def reader(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def on_message(message: Any) -> None:
            raw = json.loads(message.value)
            if raw.get("correlation_id") != str(correlation):
                return
            seen.append({"topic": topic, "event": raw.get("payload", raw)})
            if topic == terminal:
                done.set()

        return on_message

    async def on_command(message: Any) -> None:
        raw = json.loads(message.value)
        if raw.get("correlation_id") == str(correlation):
            await commands.put(message.value)

    subscribe: Any = bus.subscribe
    # Kafka replays retained records for a fresh group; the in-memory bus keeps no log.
    offset = (
        {"auto_offset_reset": "earliest"}
        if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
        else {}
    )
    for topic in [command_topic, *event_topics]:
        callback = on_command if topic == command_topic else reader(topic)
        unsubscribes.append(
            await subscribe(
                topic,
                on_message=callback,
                group_id=f"claude-review-batch-proof-{run}-{topic[-24:]}",
                **offset,
            )
        )
    try:
        command = envelope_bytes_for(
            command_topic, request.model_dump(mode="json"), correlation
        )
        seen.append(
            {
                "topic": command_topic,
                "event": request.model_dump(mode="json"),
                "sent": True,
            }
        )
        await bus.publish(command_topic, str(correlation).encode(), command)
        delivered = json.loads(await asyncio.wait_for(commands.get(), wait_seconds))
        delivered_request = ModelClaudeReviewBatchRequest.model_validate(
            delivered["payload"]
        )
        handler = build_handler(BusEventSink(bus, loop, correlation))
        await asyncio.to_thread(handler.handle, delivered_request)
        await asyncio.wait_for(done.wait(), wait_seconds)
    finally:
        for unsubscribe in unsubscribes:
            await unsubscribe()
    return seen


__all__ = ["BusEventSink", "serve_and_read_back"]
