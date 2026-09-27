# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The focused test run over the bus: the lab-host side and the loop side
(OMN-19458).

Operator ruling 2026-09-25 (OMN-19458): the delegated write-run-repair loop
reaches the test run on the bus, and the run executes behind its own command
topic, handled by a node on a lab host.

* :class:`FocusedRunHost` runs on the lab docker host. It consumes the command
  topic of ``node_focused_test_run_effect``, hands each request to that node's
  def-B handler, and publishes the receipt on the node's success terminal (an
  exception or an unreadable command goes to the failure terminal).
* :class:`FocusedRunBusCaller` runs wherever the loop runs. It publishes one
  request, and resolves it from the terminal whose ``parent_envelope_id`` is
  the command's own ``envelope_id``. The correlation id cannot be the key: one
  loop sends several runs under one correlation id (fixed, prefix, mutation,
  each attempt), and a busy host is retried with the same one.

Both read their topics from the node's ``contract.yaml`` ``runtime_dispatch``
block and nowhere else.

The host processes one command at a time from a queue, so a run that takes
longer than the broker's poll interval never stalls the consumer (a consumer
that blocks in its callback past ``max.poll.interval.ms`` is dropped from its
group and the command is redelivered). A command older than
``max_command_age_seconds`` is not run: its caller has stopped waiting, so the
host answers with an ``infra_error`` receipt instead of running a container
nobody will read.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Protocol

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.nodes.node_push_validation_effect import (
    EnumFocusedTestRunStatus,
    ModelFocusedTestRunReceipt,
    ModelFocusedTestRunRequest,
)

logger = logging.getLogger(__name__)

FOCUSED_RUN_NODE = "node_focused_test_run_effect"
#: The consumer-group service token. Every host of the node shares one group,
#: so a command is run once however many lab hosts serve it.
GROUP_SERVICE = "omnimarket"
DEFAULT_MAX_COMMAND_AGE_SECONDS = 900


class ModelFocusedRunTopics(BaseModel):
    """The node's ``runtime_dispatch`` topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: str
    failure: str


class ModelFocusedRunCommandFailure(BaseModel):
    """What the host publishes on the failure terminal: the handler raised, or
    the command could not be read as a request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "failed"
    correlation_id: str = ""
    error_message: str = Field(default="", max_length=2000)


class ProtocolBusMessage(Protocol):
    """What either bus hands a subscriber: the record's bytes."""

    @property
    def value(self) -> bytes: ...


MessageCallback = Callable[[Any], Awaitable[None]]


class ProtocolLabRunBus(Protocol):
    """The two calls both sides make on the bus (in-memory or Kafka)."""

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: MessageCallback | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]: ...


class ProtocolFocusedRunHandler(Protocol):
    async def handle(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt: ...


def load_focused_run_topics(node: str = FOCUSED_RUN_NODE) -> ModelFocusedRunTopics:
    """Read the command and terminal topics from the node's contract."""
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    dispatch = contract["runtime_dispatch"]
    terminals = dispatch["terminal_events"]
    return ModelFocusedRunTopics(
        command=dispatch["command_topic"],
        success=terminals["success"],
        failure=terminals["failure"],
    )


def event_type_for(topic: str) -> str:
    """``onex.<kind>.<producer>.<event>.v<n>`` -> ``<producer>.<event>``, the
    runtime's own derivation."""
    parts = topic.split(".")
    return f"{parts[2]}.{parts[3]}" if len(parts) >= 5 else topic


def _envelope_bytes(envelope: ModelEventEnvelope[dict[str, object]]) -> bytes:
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


async def _subscribe(
    bus: ProtocolLabRunBus,
    topic: str,
    on_message: MessageCallback,
    group_id: str,
) -> Callable[[], Awaitable[None]]:
    subscribe: Callable[..., Awaitable[Callable[[], Awaitable[None]]]] = bus.subscribe
    return await subscribe(
        topic, on_message=on_message, group_id=group_id, **_subscribe_kwargs(bus)
    )


def _subscribe_kwargs(bus: ProtocolLabRunBus) -> dict[str, str]:
    """Read retained records from the start when the bus can say so.

    A fresh group on Kafka otherwise starts at the log end, and a terminal
    that lands before the group's partitions are assigned would be missed.
    Reading from the start is safe on both sides: the client keys on its own
    command's envelope id and the host refuses a command past its age limit.
    The in-memory bus keeps no log and takes no such argument.
    """
    parameters = inspect.signature(bus.subscribe).parameters
    return (
        {"auto_offset_reset": "earliest"} if "auto_offset_reset" in parameters else {}
    )


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


class FocusedRunHost:
    """Serves the focused-run command topic on the lab docker host."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: ProtocolFocusedRunHandler,
        *,
        topics: ModelFocusedRunTopics | None = None,
        group_id: str | None = None,
        max_command_age_seconds: int = DEFAULT_MAX_COMMAND_AGE_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._topics = topics or load_focused_run_topics()
        self._group_id = group_id or derive_service_group_id(
            FOCUSED_RUN_NODE, service=GROUP_SERVICE
        )
        self._max_age = max_command_age_seconds
        self._now = now
        self._queue: asyncio.Queue[ProtocolBusMessage] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self.processed = 0

    @property
    def topics(self) -> ModelFocusedRunTopics:
        return self._topics

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._work())
        self._unsubscribe = await _subscribe(
            self._bus, self._topics.command, self._enqueue, self._group_id
        )
        logger.info(
            "focused-run host serving %s as group %s",
            self._topics.command,
            self._group_id,
        )

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def drain(self) -> None:
        """Wait until every queued command has been answered."""
        await self._queue.join()

    async def _enqueue(self, message: ProtocolBusMessage) -> None:
        self._queue.put_nowait(message)

    async def _work(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._process(message)
            except Exception:  # fallback-ok: one bad command must not stop the host; it is logged and answered below where possible
                logger.exception("focused-run host: command processing failed")
            finally:
                self.processed += 1
                self._queue.task_done()

    async def _process(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("focused-run host: unreadable command: %s", exc)
            return
        if not isinstance(raw, dict):
            logger.warning("focused-run host: command is not an object")
            return
        command_id = _uuid_or_none(raw.get("envelope_id"))
        payload = raw.get("payload", raw)
        try:
            request = ModelFocusedTestRunRequest.model_validate(payload)
        except ValidationError as exc:
            await self._publish_failure(
                command_id,
                _uuid_or_none(raw.get("correlation_id")),
                f"the command is not a focused-run request: {exc}",
            )
            return

        age = self._command_age_seconds(raw)
        if age is not None and age > self._max_age:
            receipt = ModelFocusedTestRunReceipt(
                correlation_id=request.correlation_id,
                ref_role=request.ref_role,
                attempt=request.attempt,
                commit_sha=request.commit_sha,
                test_node_id=request.test_node_id,
                status=EnumFocusedTestRunStatus.INFRA_ERROR,
                detail=(
                    f"command is {int(age)}s old (limit {self._max_age}s); its "
                    "caller has stopped waiting, so it was not run"
                ),
            )
            await self._publish_receipt(command_id, receipt)
            return

        try:
            receipt = await self._handler.handle(request)
        except Exception as exc:  # fallback-ok: a handler crash is answered on the failure terminal, never swallowed
            logger.exception("focused-run handler raised")
            await self._publish_failure(
                command_id,
                uuid.UUID(request.correlation_id),
                f"{type(exc).__name__}: {exc}",
            )
            return
        await self._publish_receipt(command_id, receipt)

    def _command_age_seconds(self, raw: dict[str, object]) -> float | None:
        stamp = raw.get("envelope_timestamp")
        if not isinstance(stamp, str):
            return None
        try:
            sent = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=UTC)
        return (self._now() - sent).total_seconds()

    async def _publish_receipt(
        self, command_id: uuid.UUID | None, receipt: ModelFocusedTestRunReceipt
    ) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            correlation_id=uuid.UUID(receipt.correlation_id),
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.success),
        )
        await self._bus.publish(
            self._topics.success,
            receipt.correlation_id.encode("utf-8"),
            _envelope_bytes(envelope),
        )

    async def _publish_failure(
        self,
        command_id: uuid.UUID | None,
        correlation_id: uuid.UUID | None,
        message: str,
    ) -> None:
        failure = ModelFocusedRunCommandFailure(
            correlation_id=str(correlation_id or ""),
            error_message=message[:2000],
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=failure.model_dump(mode="json"),
            correlation_id=correlation_id,
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.failure),
        )
        await self._bus.publish(
            self._topics.failure,
            str(correlation_id or "").encode("utf-8") or None,
            _envelope_bytes(envelope),
        )


class FocusedRunBusCaller:
    """Publishes focused-run requests and resolves each from its own terminal."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        *,
        topics: ModelFocusedRunTopics | None = None,
        group_id: str | None = None,
        wait_slack_seconds: int = 900,
    ) -> None:
        self._bus = bus
        self._topics = topics or load_focused_run_topics()
        # One group per client, never shared: every client must see every
        # terminal and pick out its own.
        self._group_id = group_id or (
            f"{derive_service_group_id('delegated_test_loop_client', service=GROUP_SERVICE)}"
            f".{uuid.uuid4().hex[:12]}"
        )
        self._slack = wait_slack_seconds
        self._pending: dict[uuid.UUID, asyncio.Future[dict[str, object]]] = {}
        self._pending_topic: dict[uuid.UUID, str] = {}
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []

    async def start(self) -> None:
        for topic in (self._topics.success, self._topics.failure):
            self._unsubscribes.append(
                await _subscribe(
                    self._bus, topic, self._terminal_handler(topic), self._group_id
                )
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes.clear()

    def _terminal_handler(
        self, topic: str
    ) -> Callable[[ProtocolBusMessage], Awaitable[None]]:
        async def on_message(message: ProtocolBusMessage) -> None:
            try:
                raw = json.loads(message.value)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict):
                return
            parent = _uuid_or_none(raw.get("parent_envelope_id"))
            if parent is None:
                return
            future = self._pending.get(parent)
            if future is None or future.done():
                return
            self._pending_topic[parent] = topic
            future.set_result(raw)

        return on_message

    async def run(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=request.model_dump(mode="json"),
            correlation_id=uuid.UUID(request.correlation_id),
            event_type=event_type_for(self._topics.command),
        )
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[envelope.envelope_id] = future
        wait = request.timeout_seconds + self._slack
        try:
            await self._bus.publish(
                self._topics.command,
                request.correlation_id.encode("utf-8"),
                _envelope_bytes(envelope),
            )
            raw = await asyncio.wait_for(future, timeout=wait)
        except TimeoutError:
            return self._infra_error(
                request,
                f"no terminal for command {envelope.envelope_id} on "
                f"{self._topics.success} within {wait}s; is a lab host serving "
                f"{self._topics.command}?",
            )
        finally:
            self._pending.pop(envelope.envelope_id, None)
        topic = self._pending_topic.pop(envelope.envelope_id, self._topics.success)
        payload = raw.get("payload")
        if topic == self._topics.failure or not isinstance(payload, dict):
            detail = (
                str(payload.get("error_message", ""))
                if isinstance(payload, dict)
                else "the terminal carried no payload"
            )
            return self._infra_error(request, f"lab host failure terminal: {detail}")
        try:
            return ModelFocusedTestRunReceipt.model_validate(payload)
        except ValidationError as exc:
            return self._infra_error(
                request, f"unreadable receipt on {self._topics.success}: {exc}"
            )

    @staticmethod
    def _infra_error(
        request: ModelFocusedTestRunRequest, detail: str
    ) -> ModelFocusedTestRunReceipt:
        return ModelFocusedTestRunReceipt(
            correlation_id=request.correlation_id,
            ref_role=request.ref_role,
            attempt=request.attempt,
            commit_sha=request.commit_sha,
            test_node_id=request.test_node_id,
            status=EnumFocusedTestRunStatus.INFRA_ERROR,
            detail=detail[:2000],
        )


__all__ = [
    "DEFAULT_MAX_COMMAND_AGE_SECONDS",
    "FOCUSED_RUN_NODE",
    "FocusedRunBusCaller",
    "FocusedRunHost",
    "ModelFocusedRunCommandFailure",
    "ModelFocusedRunTopics",
    "ProtocolFocusedRunHandler",
    "ProtocolLabRunBus",
    "event_type_for",
    "load_focused_run_topics",
]
