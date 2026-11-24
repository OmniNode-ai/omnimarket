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
from omnimarket.enums.enum_agent_message_verification_reason import (
    EnumAgentMessageVerificationReason,
)
from omnimarket.handlers.agent_message.signature import sign_message
from omnimarket.handlers.bus_binding.node_broker import NodeBrokerResolutionError
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.models.agent_message.model_agent_message_rejection import (
    ModelAgentMessageRejection,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message_verify import (
    AgentMessageVerificationError,
    AgentMessageVerifier,
    classify_validation_error,
    validation_detail,
)
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_request import (
    ModelAgentMessageRequest,
)
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_result import (
    ModelAgentMessageResult,
)
from omnimarket.nodes.node_agent_message_effect.protocols import (
    ProtocolAgentMessageBus,
    ProtocolAgentMessageEmitter,
    ProtocolAgentSenderRegistry,
    ProtocolAgentSigningKeys,
)
from omnimarket.nodes.node_event_emit_effect import ModelEmitRequest

EVENT_TYPE = "agent.message"
_WIRE_FIELDS = (
    "from",
    "to",
    "thread",
    "kind",
    "text",
    "sent_at",
    "platform",
    "message_id",
    "message_schema_version",
    "sender_proof",
)
SCHEMA_VERSION = 1
RECIPIENT_ALL = "all"
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
    """The event id: stable across retries, so a retry of the same message keeps one spool record.

    A message with a ``message_id`` hashes that id alone (its ``sent_at`` is re-stamped by a retry and must not
    change the id). A legacy message with none keeps the content hash of the whole wire message.
    """
    if message.message_id is not None:
        return hashlib.sha256(
            f"agent-message:{message.message_id}".encode()
        ).hexdigest()
    wire = message.model_dump(mode="json", by_alias=True, exclude_none=True)
    return hashlib.sha256(
        json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def decode_message(value: bytes) -> ModelAgentMessage:
    """One topic record as a typed message; ``AgentMessageVerificationError`` (a ValueError) when it is not one."""
    try:
        decoded = json.loads(value)
    except ValueError as exc:
        raise AgentMessageVerificationError(
            EnumAgentMessageVerificationReason.MALFORMED_MESSAGE, "record is not JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise AgentMessageVerificationError(
            EnumAgentMessageVerificationReason.MALFORMED_MESSAGE,
            "record is not an object",
        )
    payload = decoded.get("payload", decoded)
    if not isinstance(payload, dict):
        raise AgentMessageVerificationError(
            EnumAgentMessageVerificationReason.MALFORMED_MESSAGE,
            "payload is not an object",
        )
    try:
        return ModelAgentMessage.model_validate(
            {key: payload[key] for key in _WIRE_FIELDS if key in payload}
        )
    except ValidationError as exc:
        raise AgentMessageVerificationError(
            classify_validation_error(exc), validation_detail(exc)
        ) from exc


class HandlerAgentMessage:
    """Send one agent message, or read the messages addressed to one recipient."""

    def __init__(
        self,
        *,
        emitter: ProtocolAgentMessageEmitter | None = None,
        bus: ProtocolAgentMessageBus | None = None,
        bus_factory: Callable[[], ProtocolAgentMessageBus] | None = None,
        registry: ProtocolAgentSenderRegistry | None = None,
        signing_keys: ProtocolAgentSigningKeys | None = None,
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
        self._registry = registry
        self._signing_keys = signing_keys
        self._directory_owned = registry is None

    async def handle(
        self, request: ModelAgentMessageRequest
    ) -> ModelAgentMessageResult:
        if request.operation is EnumAgentMessageOperation.SEND:
            return await self._send(request)
        return await self._read(request)

    def _resolve_directory(self) -> ProtocolAgentSenderRegistry:
        """The injected registry, or the settings store's when none was injected (which also supplies the keys)."""
        if self._registry is None:
            from omnimarket.nodes.node_agent_message_effect.container.container_agent_message_effect import (
                create_directory,
            )

            directory = create_directory()
            self._registry = directory
            if self._directory_owned and self._signing_keys is None:
                self._signing_keys = directory
        return self._registry

    def _refuse(
        self,
        operation: EnumAgentMessageOperation,
        refusal: EnumAgentMessageRefusal,
        detail: str,
        reason: EnumAgentMessageVerificationReason | None = None,
    ) -> ModelAgentMessageResult:
        return ModelAgentMessageResult(
            operation=operation,
            accepted=False,
            refusal=refusal,
            detail=detail,
            reason=reason,
        )

    async def _sign(self, message: ModelAgentMessage) -> ModelAgentMessage:
        """Fix the id and version, then sign with the key this host holds for the declared sender.

        A caller that sent no ``message_id`` gets the content hash of the message as its id, so an identical resend
        keeps one id; a caller that chose an id keeps it across retries whatever ``sent_at`` is re-stamped to.

        A sender this host holds no key for goes out in the legacy shape while the cutover allows it, and is
        refused as an unknown sender once it does not.
        """
        message = message.model_copy(
            update={
                "message_id": message.message_id or message_event_id(message),
                "message_schema_version": message.message_schema_version
                or SCHEMA_VERSION,
            }
        )
        if message.sender_proof is not None:
            return message
        registry = self._resolve_directory()
        keys = self._signing_keys
        key = await keys.signing_key(message.sender) if keys is not None else None
        if key is not None:
            return message.model_copy(
                update={"sender_proof": sign_message(message, key)}
            )
        if keys is not None and not await registry.accepts_legacy():
            raise AgentMessageVerificationError(
                EnumAgentMessageVerificationReason.UNKNOWN_SENDER,
                f"this host holds no signing key for {message.sender!r}",
            )
        return message

    async def _send(self, request: ModelAgentMessageRequest) -> ModelAgentMessageResult:
        operation = EnumAgentMessageOperation.SEND
        try:
            message = ModelAgentMessage.model_validate(request.message)
        except ValidationError as exc:
            return self._refuse(
                operation,
                EnumAgentMessageRefusal.INVALID_MESSAGE,
                validation_detail(exc),
                classify_validation_error(exc),
            )
        try:
            message = await self._sign(message)
        except AgentMessageVerificationError as exc:
            return self._refuse(
                operation,
                EnumAgentMessageRefusal.INVALID_MESSAGE,
                exc.detail,
                exc.reason,
            )
        except (
            Exception
        ) as exc:  # effect boundary: an unreadable store or key is a typed result
            return self._refuse(
                operation, EnumAgentMessageRefusal.PUBLISH_FAILED, repr(exc)
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
                        **message.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
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
        try:
            return await self._select(request, records)
        except Exception as exc:  # effect boundary: an unreadable registry fails the read, never a partial answer
            return self._refuse(
                operation,
                EnumAgentMessageRefusal.READ_FAILED,
                f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
            )

    async def _select(
        self, request: ModelAgentMessageRequest, records: list[bytes]
    ) -> ModelAgentMessageResult:
        """Decode every record, keep those for the recipient, and return only the ones whose sender verifies."""
        verifier = AgentMessageVerifier(self._resolve_directory())
        matched: dict[str, ModelAgentMessage] = {}
        rejections: list[ModelAgentMessageRejection] = []
        skipped = 0
        unverified = 0
        for value in records:
            try:
                message = decode_message(value)
            except AgentMessageVerificationError as exc:
                skipped += 1
                rejections.append(
                    ModelAgentMessageRejection(reason=exc.reason, detail=exc.detail)
                )
                continue
            if message.to not in (request.to, RECIPIENT_ALL):
                continue
            if request.thread is not None and message.thread != request.thread:
                continue
            if request.since is not None and message.sent_at < request.since:
                continue
            try:
                verified = await verifier.verify(message)
            except AgentMessageVerificationError as exc:
                rejections.append(
                    ModelAgentMessageRejection(
                        reason=exc.reason,
                        detail=exc.detail,
                        sender=message.sender,
                        message_id=message.message_id,
                    )
                )
                continue
            event_id = message_event_id(message)
            if event_id not in matched and not verified:
                unverified += 1
            matched.setdefault(event_id, message)
        ordered = sorted(matched.values(), key=lambda m: m.sent_at)
        return ModelAgentMessageResult(
            operation=EnumAgentMessageOperation.READ,
            accepted=True,
            topic=self._topic,
            messages=tuple(ordered[-request.limit :]),
            skipped=skipped,
            unverified=unverified,
            rejections=tuple(rejections),
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
