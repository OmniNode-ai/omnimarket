# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler: send and read agent messages on the one declared topic.

The topic, the reader's group prefix and the read window come from this node's contract; no topic string is
spelled here. ``send`` validates the message, then hands one event to the emit effect (the durable spool,
drained to the declared broker); an invalid message is refused with a typed result and nothing reaches the
emitter. ``read`` takes a fresh consumer group, scans the topic from the earliest offset for one bounded
window and returns the typed messages for the recipient, oldest first.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from importlib import resources
from typing import Any
from uuid import uuid4

import yaml
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import ValidationError

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation
from omnimarket.enums.enum_agent_message_refusal import EnumAgentMessageRefusal
from omnimarket.handlers.bus_binding.node_broker import NodeBrokerResolutionError
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_request import (
    ModelAgentMessageRequest,
)
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_result import (
    ModelAgentMessageResult,
)
from omnimarket.nodes.node_agent_message_effect.protocols import (
    ProtocolAgentMessageBus,
    ProtocolAgentMessageEmitter,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)

EVENT_TYPE = "agent.message"
_WIRE_FIELDS = ("from", "to", "thread", "kind", "text", "sent_at", "platform")
_QUIET_GAP_SECONDS = 1.0


def load_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(
        resources.files("omnimarket.nodes.node_agent_message_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    if not isinstance(loaded, dict):
        raise ValueError("node_agent_message_effect contract.yaml is not a mapping")
    return loaded


def message_event_id(message: ModelAgentMessage) -> str:
    """The content hash of the wire message: a retry of the same message keeps one spool record."""
    wire = message.model_dump(mode="json", by_alias=True)
    return hashlib.sha256(
        json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def decode_message(value: bytes) -> ModelAgentMessage:
    """One topic record as a typed message; ValueError when it is not one."""
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("record is not an object")
    payload = decoded.get("payload", decoded)
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    try:
        return ModelAgentMessage.model_validate(
            {key: payload[key] for key in _WIRE_FIELDS if key in payload}
        )
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _invalid_detail(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'message'}: {err['msg']}"
        for err in exc.errors()
    )


class HandlerAgentMessage:
    """Send one agent message, or read the messages addressed to one recipient."""

    def __init__(
        self,
        *,
        emitter: ProtocolAgentMessageEmitter | None = None,
        bus: ProtocolAgentMessageBus | None = None,
        bus_factory: Callable[[], ProtocolAgentMessageBus] | None = None,
        read_window_seconds: float | None = None,
        quiet_gap_seconds: float = _QUIET_GAP_SECONDS,
    ) -> None:
        contract = load_contract()
        self._topic: str = contract["event_bus"]["publish_topics"][0]
        subscription = contract["caller_subscription"]
        if subscription["topics"] != [self._topic]:
            raise ValueError("agent message contract: read and publish topics differ")
        self._group_prefix: str = subscription["group_id_prefix"]
        self._auto_offset_reset: str = subscription["auto_offset_reset"]
        self._window = float(
            read_window_seconds
            if read_window_seconds is not None
            else subscription["read_window_seconds"]
        )
        self._quiet_gap = quiet_gap_seconds
        self._emitter = emitter
        self._bus = bus
        self._bus_factory = bus_factory

    async def handle(
        self, request: ModelAgentMessageRequest
    ) -> ModelAgentMessageResult:
        if request.operation is EnumAgentMessageOperation.SEND:
            return self._send(request)
        return await self._read(request)

    def _refuse(
        self,
        operation: EnumAgentMessageOperation,
        refusal: EnumAgentMessageRefusal,
        detail: str,
    ) -> ModelAgentMessageResult:
        return ModelAgentMessageResult(
            operation=operation, accepted=False, refusal=refusal, detail=detail
        )

    def _send(self, request: ModelAgentMessageRequest) -> ModelAgentMessageResult:
        operation = EnumAgentMessageOperation.SEND
        try:
            message = ModelAgentMessage.model_validate(request.message)
        except ValidationError as exc:
            return self._refuse(
                operation, EnumAgentMessageRefusal.INVALID_MESSAGE, _invalid_detail(exc)
            )
        event_id = message_event_id(message)
        try:
            if self._emitter is None:
                from omnimarket.nodes.node_agent_message_effect.container.container_agent_message_effect import (
                    create_emitter,
                )

                self._emitter = create_emitter()
            emitted = self._emitter.handle(
                ModelEmitRequest(
                    event_type=EVENT_TYPE,
                    payload={
                        **message.model_dump(mode="json", by_alias=True),
                        "event_id": event_id,
                    },
                    correlation_id=event_id,
                    event_id=event_id,
                )
            )
        except NodeBrokerResolutionError as exc:
            return self._refuse(
                operation, EnumAgentMessageRefusal.BROKER_NOT_DECLARED, str(exc)
            )
        except (
            Exception
        ) as exc:  # effect boundary: any spool or publish refusal is a typed result
            return self._refuse(
                operation, EnumAgentMessageRefusal.PUBLISH_FAILED, repr(exc)
            )
        return ModelAgentMessageResult(
            operation=operation,
            accepted=True,
            published=emitted.published,
            topic=self._topic,
            event_id=event_id,
        )

    async def _read(self, request: ModelAgentMessageRequest) -> ModelAgentMessageResult:
        operation = EnumAgentMessageOperation.READ
        owned = False
        try:
            bus = self._bus
            if bus is None:
                factory = self._bus_factory
                if factory is None:
                    from omnimarket.nodes.node_agent_message_effect.container.container_agent_message_effect import (
                        create_bus,
                    )

                    factory = create_bus
                bus = factory()
                owned = True
        except NodeBrokerResolutionError as exc:
            return self._refuse(
                operation, EnumAgentMessageRefusal.BROKER_NOT_DECLARED, str(exc)
            )
        try:
            records = await self._scan(bus)
        except NodeBrokerResolutionError as exc:
            return self._refuse(
                operation, EnumAgentMessageRefusal.BROKER_NOT_DECLARED, str(exc)
            )
        except Exception as exc:  # effect boundary: a failed scan is a typed result, never a partial answer
            return self._refuse(
                operation,
                EnumAgentMessageRefusal.READ_FAILED,
                f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
            )
        finally:
            close = getattr(bus, "close", None) if owned else None
            if close is not None:
                await close()
        matched: dict[str, ModelAgentMessage] = {}
        skipped = 0
        for value in records:
            try:
                message = decode_message(value)
            except ValueError:
                skipped += 1
                continue
            if message.to != request.to:
                continue
            if request.thread is not None and message.thread != request.thread:
                continue
            if request.since is not None and message.sent_at < request.since:
                continue
            matched.setdefault(message_event_id(message), message)
        ordered = sorted(matched.values(), key=lambda m: m.sent_at)
        return ModelAgentMessageResult(
            operation=operation,
            accepted=True,
            topic=self._topic,
            messages=tuple(ordered[-request.limit :]),
            skipped=skipped,
        )

    async def _scan(self, bus: ProtocolAgentMessageBus) -> list[bytes]:
        """Every record on the topic that arrives in one bounded window, from a fresh consumer group."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._window
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        async def on_message(message: ModelEventMessage) -> None:
            queue.put_nowait(bytes(message.value))

        records: list[bytes] = []
        stop = await asyncio.wait_for(
            bus.subscribe(
                self._topic,
                on_message=on_message,
                group_id=f"{self._group_prefix}-{uuid4().hex}",
                auto_offset_reset=self._auto_offset_reset,
            ),
            timeout=self._window,
        )
        try:
            # Until the first record the whole window is allowed (a fresh group joins slowly); after it, a quiet gap ends the scan.
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                wait = min(self._quiet_gap, remaining) if records else remaining
                try:
                    records.append(await asyncio.wait_for(queue.get(), timeout=wait))
                except TimeoutError:
                    break
        finally:
            await stop()
        while not queue.empty():
            records.append(queue.get_nowait())
        return records


__all__: list[str] = [
    "EVENT_TYPE",
    "HandlerAgentMessage",
    "decode_message",
    "message_event_id",
]
