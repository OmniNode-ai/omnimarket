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
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from functools import partial
from importlib import resources
from typing import Protocol

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import ProjectionNotMaterializedError
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
    ModelWorkLedgerTerminalRefused,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers import terminal_refused
from omnimarket.nodes.node_work_ledger_bus_mirror import (
    HandlerWorkLedgerBusMirror,
    ModelWorkLedgerBusMirrorRequest,
)
from omnimarket.nodes.node_work_ledger_delegation_mirror import (
    HandlerWorkLedgerDelegationMirror,
)
from omnimarket.projection.envelope import unwrap_envelope

logger = logging.getLogger(__name__)
WORK_LEDGER_APPEND_NODE = "node_work_ledger_append_effect"
GROUP_SERVICE = "omnimarket"
WORK_LEDGER_MIRROR_NODE = "node_work_ledger_bus_mirror"
WORK_LEDGER_DELEGATION_NODE = "node_work_ledger_delegation_mirror"


def load_work_ledger_mirror_topics() -> tuple[str, ...]:
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{WORK_LEDGER_MIRROR_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return tuple(contract["subscriptions"]["topics"])


def load_work_ledger_delegation_topics() -> tuple[str, ...]:
    """Completed topic first, failed second, as the contract declares them."""
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{WORK_LEDGER_DELEGATION_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return tuple(contract["runtime_dispatch"]["subscribe_topics"])


class ModelWorkLedgerAppendTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: str
    failure: str
    terminal_refused: str
    writer_command_topic_template: str
    writer_principal_pattern: str

    def writer_command_topic(self, principal: str) -> str:
        if re.fullmatch(self.writer_principal_pattern, principal) is None:
            raise ValueError(f"invalid writer principal {principal!r}")
        return self.writer_command_topic_template.format(principal=principal)


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
        self,
        request: ModelWorkLedgerAppendRequest,
        *,
        writer_principal: str | None = None,
    ) -> ModelWorkLedgerAppendReceipt: ...


def load_work_ledger_append_topics() -> ModelWorkLedgerAppendTopics:
    text = (
        resources.files(f"omnimarket.nodes.{WORK_LEDGER_APPEND_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    dispatch = contract["runtime_dispatch"]
    published = {
        entry["event_type"]: entry["topic"] for entry in contract["published_events"]
    }
    writer = contract["writer_topics"]
    return ModelWorkLedgerAppendTopics(
        writer_command_topic_template=(
            f"onex.{writer['kind']}.{writer['producer']}."
            f"{writer['name_prefix']}{{principal}}.v{writer['version']}"
        ),
        writer_principal_pattern=writer["principal_pattern"],
        command=dispatch["command_topic"],
        success=dispatch["terminal_events"]["success"],
        failure=dispatch["terminal_events"]["failure"],
        terminal_refused=published[ModelWorkLedgerTerminalRefused.__name__],
    )


class WorkLedgerAppendHost:
    """Serve ledger commands strictly one at a time in one consumer group."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: ProtocolLedgerAppendHandler,
        *,
        writer_principals: Iterable[str],
        operator_principal: str | None = None,
        topics: ModelWorkLedgerAppendTopics | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._operator_principal = operator_principal
        self._topics = topics or load_work_ledger_append_topics()
        self._writer_principals = frozenset(writer_principals)
        for principal in self._writer_principals:
            self._topics.writer_command_topic(principal)
        self._mirror = HandlerWorkLedgerBusMirror()
        self._delegation_topics = load_work_ledger_delegation_topics()
        self._delegation_mirror = HandlerWorkLedgerDelegationMirror()
        self._queue: asyncio.Queue[
            tuple[
                ProtocolBusMessage, bool, str | None, str | None, asyncio.Future[None]
            ]
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
                partial(self._enqueue, writer_principal=None),
                derive_service_group_id(WORK_LEDGER_APPEND_NODE, service=GROUP_SERVICE),
                "earliest",
            )
        )
        for principal in sorted(self._writer_principals):
            self._unsubscribes.append(
                await _subscribe(
                    self._bus,
                    self._topics.writer_command_topic(principal),
                    partial(self._enqueue, writer_principal=principal),
                    derive_service_group_id(
                        WORK_LEDGER_APPEND_NODE, service=GROUP_SERVICE
                    ),
                    "earliest",
                )
            )
        if self._operator_principal is None:
            logger.warning(
                "work-ledger host: no operator principal, terminals not mirrored"
            )
            return
        group = derive_service_group_id(WORK_LEDGER_MIRROR_NODE, service=GROUP_SERVICE)
        for topic in load_work_ledger_mirror_topics():
            self._unsubscribes.append(
                await _subscribe(
                    self._bus,
                    topic,
                    self._enqueue_mirror,
                    group,
                    "earliest",
                )
            )
        delegation_group = derive_service_group_id(
            WORK_LEDGER_DELEGATION_NODE, service=GROUP_SERVICE
        )
        for topic in self._delegation_topics:
            self._unsubscribes.append(
                await _subscribe(
                    self._bus,
                    topic,
                    partial(self._enqueue_delegation, delegation_topic=topic),
                    delegation_group,
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
        self, message: ProtocolBusMessage, *, writer_principal: str | None
    ) -> None:
        # Handled inside the subscription callback, not handed to a queue: the bus
        # client advances the group's position when the callback returns, so a
        # command must be appended and answered first. Committing earlier is the
        # model's commit_first control, where a crash loses the command
        # (omnibase_internal tla/ledger_bus_append).
        await self._submit(message, mirror=False, writer_principal=writer_principal)

    async def _enqueue_mirror(self, message: ProtocolBusMessage) -> None:
        await self._submit(
            message, mirror=True, writer_principal=self._operator_principal
        )

    async def _enqueue_delegation(
        self, message: ProtocolBusMessage, *, delegation_topic: str
    ) -> None:
        await self._submit(
            message,
            mirror=True,
            delegation_topic=delegation_topic,
            writer_principal=self._operator_principal,
        )

    async def _submit(
        self,
        message: ProtocolBusMessage,
        *,
        mirror: bool,
        writer_principal: str | None,
        delegation_topic: str | None = None,
    ) -> None:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(
            (message, mirror, delegation_topic, writer_principal, done)
        )
        await done

    async def _worker(self) -> None:
        while True:
            (
                message,
                mirror,
                delegation_topic,
                writer_principal,
                done,
            ) = await self._queue.get()
            try:
                await self._process(
                    message,
                    mirror=mirror,
                    delegation_topic=delegation_topic,
                    writer_principal=writer_principal,
                )
            except Exception as exc:  # fallback-ok: callback fails for redelivery; worker serves the next request
                logger.exception("work-ledger host: command processing failed")
                if not done.done():
                    # Kafka treats generic callback exceptions as bounded retry /
                    # DLQ, which can advance past a valid unit still owed a row.
                    # Use its existing persistence barrier for mirror failures.
                    done.set_exception(
                        ProjectionNotMaterializedError(
                            f"mirrored ledger row not materialized: {exc}",
                            projection_type=WORK_LEDGER_MIRROR_NODE,
                        )
                        if mirror
                        else exc
                    )
            else:
                if not done.done():
                    done.set_result(None)
            finally:
                self.processed += 1
                self._queue.task_done()

    async def _process(
        self,
        message: ProtocolBusMessage,
        *,
        writer_principal: str | None,
        mirror: bool = False,
        delegation_topic: str | None = None,
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
            if mirror:
                payload = raw.get("payload")
                if not isinstance(payload, dict):
                    await self._publish_failure(
                        command_id, "", "lab terminal has no payload"
                    )
                    return
                mapped = ModelWorkLedgerBusMirrorRequest.model_validate(
                    {
                        **payload,
                        "envelope_timestamp": raw.get("envelope_timestamp"),
                    }
                )
                request = self._mirror.handle(mapped)
            else:
                payload = raw.get("payload", raw)
                if writer_principal is None and isinstance(payload, dict):
                    # Legacy attribution fields are discarded only to answer a
                    # parsed command with an unattributed refusal.
                    payload = {
                        key: value
                        for key, value in payload.items()
                        if key not in {"principal", "signature"}
                    }
                request = ModelWorkLedgerAppendRequest.model_validate(payload)
        except ValidationError as exc:
            await self._publish_failure(
                command_id, "", f"not a ledger append request: {exc}"
            )
            return
        await self._append_and_publish(
            request, command_id, mirror=mirror, writer_principal=writer_principal
        )

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
            if (terminal.status == "completed") != (
                topic == self._delegation_topics[0]
            ):
                raise ValueError("delegation terminal status disagrees with topic")
            request = self._delegation_mirror.handle(terminal)
        except (ValueError, ValidationError) as exc:
            await self._publish_failure(
                command_id, "", f"invalid delegation terminal: {exc}"
            )
            return
        await self._append_and_publish(
            request, command_id, mirror=True, writer_principal=self._operator_principal
        )

    async def _append_and_publish(
        self,
        request: ModelWorkLedgerAppendRequest,
        command_id: uuid.UUID | None,
        *,
        mirror: bool,
        writer_principal: str | None,
    ) -> None:
        try:
            receipt = await asyncio.to_thread(
                self._handler.handle, request, writer_principal=writer_principal
            )
        except (
            Exception
        ) as exc:  # fallback-ok: a handler crash is answered on the failure terminal
            await self._publish_failure(
                command_id, str(request.request_id), f"{type(exc).__name__}: {exc}"
            )
            if mirror:
                raise
            return
        # ProjectionNotMaterializedError's doctrine: content refusals advance;
        # write-path errors withhold the offset and must not publish a receipt.
        if mirror and receipt.status == EnumWorkLedgerAppendStatus.ERROR:
            raise RuntimeError(
                f"mirrored receipt append {receipt.status}: {receipt.message}"
            )
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
        refused = terminal_refused(request, receipt, datetime.now(UTC))
        if refused is not None:
            await self._publish_terminal_refused(refused, command_id)
        if mirror and receipt.status == EnumWorkLedgerAppendStatus.REFUSED:
            logger.warning(
                "work-ledger host %s request %s refused: %s",
                receipt.ledger_host,
                receipt.request_id,
                receipt.message,
            )
            return
        logger.info(
            "work-ledger host %s request %s status=%s exit=%s lines=%s",
            receipt.ledger_host,
            receipt.request_id,
            receipt.status,
            receipt.exit_code,
            receipt.ledger_lines,
        )

    async def _publish_terminal_refused(
        self, refused: ModelWorkLedgerTerminalRefused, command_id: uuid.UUID | None
    ) -> None:
        """A refused TERMINAL leaves its CLAIM open: say so on the bus and in the log."""
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=refused.model_dump(mode="json"),
            correlation_id=refused.request_id,
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.terminal_refused),
        )
        await self._bus.publish(
            self._topics.terminal_refused,
            str(refused.request_id).encode("utf-8"),
            _bytes(envelope),
        )
        logger.error(
            "work-ledger host %s TERMINAL_REFUSED request %s lanes=%s tickets=%s "
            "prs=%s: %s; the CLAIMs these rows close stay open until their lease TTL",
            refused.ledger_host,
            refused.request_id,
            ",".join(refused.terminal_lanes) or "-",
            ",".join(refused.tickets) or "-",
            ",".join(refused.prs) or "-",
            refused.reason,
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
        principal: str,
    ) -> None:
        self._bus = bus
        self._topics = topics or load_work_ledger_append_topics()
        self._command_topic = self._topics.writer_command_topic(principal)
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
            event_type=event_type_for(self._command_topic),
        )
        future: asyncio.Future[tuple[str, dict[str, object]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[envelope.envelope_id] = future
        try:
            await self._bus.publish(
                self._command_topic,
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
