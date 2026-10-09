# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger-host and caller sides of ledger append commands (OMN-20275).

Like lab work, topics come only from the node contract, and terminals name
their command's envelope as parent. One host group and one queue worker
serialize commands; each caller uses its own terminal consumer group.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from importlib import resources
from typing import Protocol

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.lab_work.bus import _bytes, _subscribe, _uuid_or_none
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_bus_mirror import HandlerWorkLedgerBusMirror
from omnimarket.projection.envelope import unwrap_envelope

logger = logging.getLogger(__name__)
WORK_LEDGER_APPEND_NODE = "node_work_ledger_append_effect"
GROUP_SERVICE = "omnimarket"
WORK_LEDGER_MIRROR_NODE = "node_work_ledger_bus_mirror"


class ModelWorkLedgerAppendTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: str
    failure: str


class ModelWorkLedgerAppendCommandFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "error"
    request_id: str = ""
    ledger_host: str
    error_message: str = Field(max_length=2000)


class ProtocolLedgerAppendHandler(Protocol):
    @property
    def host_name(self) -> str: ...

    def handle(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt: ...


def load_work_ledger_append_topics() -> ModelWorkLedgerAppendTopics:
    text = (
        resources.files(f"omnimarket.nodes.{WORK_LEDGER_APPEND_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = yaml.safe_load(text)["runtime_dispatch"]
    return ModelWorkLedgerAppendTopics(
        command=dispatch["command_topic"],
        success=dispatch["terminal_events"]["success"],
        failure=dispatch["terminal_events"]["failure"],
    )


def load_work_ledger_delegation_topics() -> tuple[str, ...]:
    contract = (
        resources.files(f"omnimarket.nodes.{WORK_LEDGER_MIRROR_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return tuple(yaml.safe_load(contract)["runtime_dispatch"]["subscribe_topics"])


class WorkLedgerAppendHost:
    """Serve ledger commands strictly one at a time in one consumer group."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: ProtocolLedgerAppendHandler,
        *,
        topics: ModelWorkLedgerAppendTopics | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._topics = topics or load_work_ledger_append_topics()
        self._delegation_topics = load_work_ledger_delegation_topics()
        self._mapper = HandlerWorkLedgerBusMirror()
        self._queue: asyncio.Queue[
            tuple[ProtocolBusMessage, asyncio.Future[None], str | None]
        ] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []
        self.processed = 0

    @property
    def topics(self) -> ModelWorkLedgerAppendTopics:
        return self._topics

    @property
    def delegation_topics(self) -> tuple[str, ...]:
        return self._delegation_topics

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._worker())
        self._unsubscribes.append(
            await _subscribe(
                self._bus,
                self._topics.command,
                self._enqueue,
                derive_service_group_id(WORK_LEDGER_APPEND_NODE, service=GROUP_SERVICE),
                "earliest",
            )
        )
        for topic in self._delegation_topics:
            self._unsubscribes.append(
                await _subscribe(
                    self._bus,
                    topic,
                    partial(self._enqueue, delegation_topic=topic),
                    derive_service_group_id(
                        WORK_LEDGER_MIRROR_NODE, service=GROUP_SERVICE
                    ),
                    "earliest",
                )
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def drain(self) -> None:
        await self._queue.join()

    async def _enqueue(
        self, message: ProtocolBusMessage, *, delegation_topic: str | None = None
    ) -> None:
        # Handled inside the subscription callback, not handed to a queue: the bus
        # client advances the group's position when the callback returns, so a
        # command must be appended and answered first. Committing earlier is the
        # model's commit_first control, where a crash loses the command
        # (omnibase_internal tla/ledger_bus_append).
        completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((message, completed, delegation_topic))
        await completed

    async def _worker(self) -> None:
        while True:
            message, completed, delegation_topic = await self._queue.get()
            try:
                await self._process(message, delegation_topic=delegation_topic)
            except Exception as exc:
                # A mirror failure must reach the callback so Kafka cannot pass
                # a terminal whose append has not landed. Keep the worker alive.
                logger.exception("work-ledger host: command processing failed")
                if not completed.done():
                    completed.set_exception(exc)
            else:
                if not completed.done():
                    completed.set_result(None)
            finally:
                self.processed += 1
                self._queue.task_done()

    async def _process(
        self, message: ProtocolBusMessage, *, delegation_topic: str | None = None
    ) -> None:
        if delegation_topic is not None:
            await self._process_delegation(message, delegation_topic)
            return
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await self._publish_failure(None, "", f"unreadable command: {exc}")
            return
        if not isinstance(raw, dict):
            await self._publish_failure(None, "", "command is not an object")
            return
        command_id = _uuid_or_none(raw.get("envelope_id"))
        try:
            if command_id is None:
                await self._publish_failure(
                    None, "", "command has no valid envelope_id"
                )
                return
            request = ModelWorkLedgerAppendRequest.model_validate(
                raw.get("payload", raw)
            )
        except ValidationError as exc:
            await self._publish_failure(
                command_id, "", f"not a ledger append request: {exc}"
            )
            return
        await self._append_and_publish(request, command_id)

    async def _process_delegation(
        self, message: ProtocolBusMessage, topic: str
    ) -> None:
        payload = unwrap_envelope(message.value)
        raw = payload.get("_envelope", {}) if payload is not None else {}
        if not isinstance(raw, dict):
            raw = {}
        command_id = _uuid_or_none(raw.get("envelope_id"))
        try:
            if payload is None:
                raise ValueError("delegation terminal is not an object")
            if (
                not any(
                    payload.get(key) is not None
                    for key in ("emitted_at", "emittedAt", "timestamp")
                )
                and raw.get("envelope_timestamp") is None
            ):
                raise ValueError("delegation terminal has no event timestamp")
            terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
            completed_topic = self._delegation_topics[0]
            if (terminal.status == "completed") != (topic == completed_topic):
                raise ValueError("delegation terminal status disagrees with topic")
            request = self._mapper.handle(terminal)
        except (ValueError, ValidationError) as exc:
            await self._publish_failure(
                command_id, "", f"invalid delegation terminal: {exc}"
            )
            return
        await self._append_and_publish(request, command_id, require_landed=True)

    async def _append_and_publish(
        self,
        request: ModelWorkLedgerAppendRequest,
        command_id: uuid.UUID | None,
        *,
        require_landed: bool = False,
    ) -> None:
        try:
            receipt = await asyncio.to_thread(self._handler.handle, request)
        except (
            Exception
        ) as exc:  # fallback-ok: a handler crash is answered on the failure terminal
            await self._publish_failure(
                command_id, str(request.request_id), f"{type(exc).__name__}: {exc}"
            )
            if require_landed:
                raise
            return
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            correlation_id=request.request_id,
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.success),
        )
        await self._bus.publish(
            self._topics.success,
            str(request.request_id).encode("utf-8"),
            _bytes(envelope),
        )
        logger.info(
            "work-ledger host %s request %s status=%s exit=%s lines=%s",
            receipt.ledger_host,
            receipt.request_id,
            receipt.status,
            receipt.exit_code,
            receipt.ledger_lines,
        )
        if require_landed and receipt.status not in (
            EnumWorkLedgerAppendStatus.ACCEPTED,
            EnumWorkLedgerAppendStatus.DUPLICATE,
        ):
            raise RuntimeError(
                f"delegation ledger append {receipt.status}: {receipt.message}"
            )

    async def _publish_failure(
        self, command_id: uuid.UUID | None, request_id: str, message: str
    ) -> None:
        failure = ModelWorkLedgerAppendCommandFailure(
            request_id=request_id,
            ledger_host=self._handler.host_name,
            error_message=message[-2000:],
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=failure.model_dump(mode="json"),
            correlation_id=_uuid_or_none(request_id) or uuid.uuid4(),
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.failure),
        )
        await self._bus.publish(
            self._topics.failure,
            (request_id or self._handler.host_name).encode("utf-8"),
            _bytes(envelope),
        )
        logger.warning(
            "work-ledger host %s request %s failed: %s",
            self._handler.host_name,
            request_id,
            message,
        )


class WorkLedgerAppendCaller:
    """Publish an append and await the terminal naming its command envelope."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        *,
        topics: ModelWorkLedgerAppendTopics | None = None,
    ) -> None:
        self._bus = bus
        self._topics = topics or load_work_ledger_append_topics()
        self._group = (
            f"{derive_service_group_id('work_ledger_append_client', service=GROUP_SERVICE)}"
            f".{uuid.uuid4().hex[:12]}"
        )
        self._pending: dict[
            uuid.UUID, asyncio.Future[tuple[str, dict[str, object]]]
        ] = {}
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []

    @property
    def topics(self) -> ModelWorkLedgerAppendTopics:
        return self._topics

    async def start(self) -> None:
        if self._unsubscribes:
            return
        for topic in (self._topics.success, self._topics.failure):
            self._unsubscribes.append(
                await _subscribe(
                    self._bus, topic, self._terminal(topic), self._group, "latest"
                )
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes.clear()

    def _terminal(self, topic: str) -> Callable[[ProtocolBusMessage], Awaitable[None]]:
        async def on_message(message: ProtocolBusMessage) -> None:
            try:
                raw = json.loads(message.value)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict):
                return
            parent = _uuid_or_none(raw.get("parent_envelope_id"))
            future = self._pending.get(parent) if parent is not None else None
            if future is not None and not future.done():
                future.set_result((topic, raw))

        return on_message

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float = 60.0
    ) -> ModelWorkLedgerAppendReceipt:
        await self.start()
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=request.model_dump(mode="json"),
            correlation_id=request.request_id,
            event_type=event_type_for(self._topics.command),
        )
        future: asyncio.Future[tuple[str, dict[str, object]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[envelope.envelope_id] = future
        try:
            await self._bus.publish(
                self._topics.command,
                request.ledger_id.encode("utf-8"),
                _bytes(envelope),
            )
            topic, raw = await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(envelope.envelope_id, None)
        payload = raw.get("payload")
        if topic == self._topics.failure or not isinstance(payload, dict):
            message = (
                str(payload.get("error_message", ""))
                if isinstance(payload, dict)
                else "no payload"
            )
            host = (
                str(payload.get("ledger_host", "")) if isinstance(payload, dict) else ""
            )
            return _error_receipt(
                request, f"ledger host failure terminal: {message}", host
            )
        try:
            return ModelWorkLedgerAppendReceipt.model_validate(payload)
        except ValidationError as exc:
            return _error_receipt(request, f"unreadable receipt: {exc}", "")


def _error_receipt(
    request: ModelWorkLedgerAppendRequest, message: str, host: str
) -> ModelWorkLedgerAppendReceipt:
    return ModelWorkLedgerAppendReceipt(
        request_id=request.request_id,
        status=EnumWorkLedgerAppendStatus.ERROR,
        exit_code=70,
        message=message[-2000:],
        ledger_lines=[],
        ledger_host=host,
        duration_ms=0,
    )
