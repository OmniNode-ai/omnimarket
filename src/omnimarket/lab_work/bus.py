# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab work unit over the bus: the pool-host side and the caller side
(OMN-20105).

* :class:`LabWorkHost` runs on each pool host (``onex lab-work serve``). It
  publishes the host's capacity advertisement every ``cadence_seconds`` and
  consumes the command topic in a consumer group of its own, so every host
  sees every command; it runs only the units addressed to its host name and
  publishes each receipt on the success terminal (a handler crash or an
  unreadable command goes to the failure terminal).
* :class:`LabWorkCaller` runs wherever the lane runs (``onex lab-work run``).
  It reads the pool's advertisements for one window, places the unit with the
  pure :func:`omnimarket.lab_work.placement.place`, publishes it, and resolves
  it from the terminal whose ``parent_envelope_id`` is the command's own
  ``envelope_id``.

Topics come from the node's ``contract.yaml`` and nowhere else. The transport
is the same bus the focused test run uses (``open_lab_run_bus``).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Protocol

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.lab_work.placement import (
    DEFAULT_MAX_LOAD_PER_CORE,
    DEFAULT_MIN_FREE_BYTES,
    EnumPlacementDecision,
    ModelPlacement,
    place,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    EnumLabWorkUnitStatus,
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
    WorkKind,
)

logger = logging.getLogger(__name__)

LAB_WORK_NODE = "node_lab_work_unit_effect"
GROUP_SERVICE = "omnimarket"
DEFAULT_MAX_COMMAND_AGE_SECONDS = 900
DEFAULT_TOOLS = (
    "git",
    "uv",
    "gh",
    "claude",
    "claude-login",
    "crush",
    "onex",
    "node",
    "docker",
)


class ModelLabWorkTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: str
    failure: str
    capacity: str
    cadence_seconds: int


class ModelLabWorkCommandFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "failed"
    work_unit_id: str = ""
    host: str = ""
    lane: str = ""
    repo: str = ""
    commit_sha: str = ""
    kind: WorkKind = "other"
    duration_seconds: float = 0.0
    error_message: str = Field(default="", max_length=2000)


class ProtocolWorkHandler(Protocol):
    @property
    def host_name(self) -> str: ...

    async def handle(
        self, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt: ...


class ProtocolAdvertiseHandler(Protocol):
    def handle(
        self, request: ModelHostCapacityProbeRequest
    ) -> ModelHostCapacityAdvertisement: ...


def load_lab_work_topics(node: str = LAB_WORK_NODE) -> ModelLabWorkTopics:
    """Read the command, terminal and capacity topics from the node contract."""
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    dispatch = contract["runtime_dispatch"]
    capacity = contract["capacity"]
    return ModelLabWorkTopics(
        command=dispatch["command_topic"],
        success=dispatch["terminal_events"]["success"],
        failure=dispatch["terminal_events"]["failure"],
        capacity=capacity["capacity_topic"],
        cadence_seconds=int(capacity["cadence_seconds"]),
    )


def _bytes(envelope: ModelEventEnvelope[dict[str, object]]) -> bytes:
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _subscribe(
    bus: ProtocolLabRunBus,
    topic: str,
    on_message: Callable[[Any], Awaitable[None]],
    group_id: str,
    offset_reset: str,
) -> Callable[[], Awaitable[None]]:
    """Subscribe; state where a fresh group starts when the bus can say so."""
    subscribe: Callable[..., Awaitable[Callable[[], Awaitable[None]]]] = bus.subscribe
    extra = (
        {"auto_offset_reset": offset_reset}
        if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
        else {}
    )
    return await subscribe(topic, on_message=on_message, group_id=group_id, **extra)


def host_group_id(host_name: str) -> str:
    """One consumer group per pool host: every host sees every command."""
    return (
        f"{derive_service_group_id(LAB_WORK_NODE, service=GROUP_SERVICE)}.{host_name}"
    )


class LabWorkHost:
    """Serves the lab work command topic and advertises capacity on one host."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        work_handler: ProtocolWorkHandler,
        advertise_handler: ProtocolAdvertiseHandler,
        *,
        topics: ModelLabWorkTopics | None = None,
        max_units: int = 1,
        rank_penalty: float = 0.0,
        tools: tuple[str, ...] = DEFAULT_TOOLS,
        max_command_age_seconds: int = DEFAULT_MAX_COMMAND_AGE_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bus = bus
        self._work = work_handler
        self._advertise = advertise_handler
        self._topics = topics or load_lab_work_topics()
        self._host = work_handler.host_name
        self._max_units = max_units
        self._rank_penalty = rank_penalty
        self._tools = tools
        self._max_age = max_command_age_seconds
        self._now = now
        self._queue: asyncio.Queue[ProtocolBusMessage] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self.running = 0
        self.processed = 0
        self.advertised = 0

    @property
    def topics(self) -> ModelLabWorkTopics:
        return self._topics

    async def start(self, *, advertise: bool = True) -> None:
        for _ in range(self._max_units):
            self._tasks.append(asyncio.create_task(self._worker()))
        if advertise:
            self._tasks.append(asyncio.create_task(self._beat()))
        self._unsubscribe = await _subscribe(
            self._bus,
            self._topics.command,
            self._enqueue,
            host_group_id(self._host),
            "earliest",
        )
        logger.info("lab-work host %s serving %s", self._host, self._topics.command)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def drain(self) -> None:
        await self._queue.join()

    async def advertise_once(self) -> ModelHostCapacityAdvertisement | None:
        """Read and publish one advertisement; an unreadable reading publishes
        nothing and is logged."""
        request = ModelHostCapacityProbeRequest(
            host_name=self._host,
            cadence_seconds=self._topics.cadence_seconds,
            tools=list(self._tools),
            running_units=self.running,
            max_units=self._max_units,
            rank_penalty=self._rank_penalty,
        )
        try:
            ad = await asyncio.to_thread(self._advertise.handle, request)
        except Exception:  # fallback-ok: an unreadable host publishes nothing, so readers see it go stale
            logger.exception(
                "lab-work host %s: capacity unreadable, nothing published", self._host
            )
            return None
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=ad.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(self._topics.capacity),
        )
        await self._bus.publish(
            self._topics.capacity, self._host.encode("utf-8"), _bytes(envelope)
        )
        self.advertised += 1
        return ad

    async def _beat(self) -> None:
        while True:
            await self.advertise_once()
            await asyncio.sleep(self._topics.cadence_seconds)

    async def _enqueue(self, message: ProtocolBusMessage) -> None:
        self._queue.put_nowait(message)

    async def _worker(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._process(message)
            except (
                Exception
            ):  # fallback-ok: one bad command must not stop the host; it is logged
                logger.exception(
                    "lab-work host %s: command processing failed", self._host
                )
            finally:
                self._queue.task_done()

    async def _process(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("lab-work host %s: unreadable command", self._host)
            return
        if not isinstance(raw, dict):
            return
        payload = raw.get("payload", raw)
        if isinstance(payload, dict) and payload.get("target_host") != self._host:
            return  # another host's unit: seen by every host, run by its own
        command_id = _uuid_or_none(raw.get("envelope_id"))
        try:
            request = ModelLabWorkUnitRequest.model_validate(payload)
        except ValidationError as exc:
            await self._publish_failure(command_id, "", f"not a lab work unit: {exc}")
            return
        age = self._age(raw)
        if age is not None and age > self._max_age:
            logger.warning(
                "lab-work host %s: unit %s is %ds old (limit %ds), not run and not answered",
                self._host,
                request.work_unit_id,
                int(age),
                self._max_age,
            )
            return
        self.running += 1
        started = time.monotonic()
        try:
            receipt = await self._work.handle(request)
        except (
            Exception
        ) as exc:  # fallback-ok: a handler crash is answered on the failure terminal
            logger.exception("lab-work handler raised")
            await self._publish_failure(
                command_id,
                request.work_unit_id,
                f"{type(exc).__name__}: {exc}",
                request=request,
                duration_seconds=time.monotonic() - started,
            )
            return
        finally:
            self.running -= 1
            self.processed += 1
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_copy(
                update={"lane": request.lane, "kind": request.kind}
            ).model_dump(mode="json"),
            correlation_id=_uuid_or_none(raw.get("correlation_id")) or uuid.uuid4(),
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.success),
        )
        await self._bus.publish(
            self._topics.success, receipt.work_unit_id.encode("utf-8"), _bytes(envelope)
        )

    def _age(self, raw: dict[str, object]) -> float | None:
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

    async def _publish_failure(
        self,
        command_id: uuid.UUID | None,
        work_unit_id: str,
        message: str,
        *,
        request: ModelLabWorkUnitRequest | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        failure = ModelLabWorkCommandFailure(
            work_unit_id=work_unit_id,
            host=self._host,
            error_message=message[:2000],
            lane=request.lane if request else "",
            repo=request.repo if request else "",
            commit_sha=request.commit_sha if request else "",
            kind=request.kind if request else "other",
            duration_seconds=duration_seconds,
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=failure.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.failure),
        )
        await self._bus.publish(
            self._topics.failure,
            (work_unit_id or self._host).encode("utf-8"),
            _bytes(envelope),
        )


class LabWorkCaller:
    """Reads advertisements, places a unit, sends it and waits for its receipt."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        *,
        topics: ModelLabWorkTopics | None = None,
        wait_slack_seconds: int = 300,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bus = bus
        self._topics = topics or load_lab_work_topics()
        suffix = uuid.uuid4().hex[:12]
        base = derive_service_group_id("lab_work_client", service=GROUP_SERVICE)
        self._group = f"{base}.{suffix}"
        self._slack = wait_slack_seconds
        self._now = now
        self._ads: list[ModelHostCapacityAdvertisement] = []
        self._pending: dict[
            uuid.UUID, asyncio.Future[tuple[str, dict[str, object]]]
        ] = {}
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []

    @property
    def topics(self) -> ModelLabWorkTopics:
        return self._topics

    async def start(self) -> None:
        self._unsubscribes.append(
            await _subscribe(
                self._bus,
                self._topics.capacity,
                self._on_ad,
                f"{self._group}.cap",
                "latest",
            )
        )
        for topic in (self._topics.success, self._topics.failure):
            self._unsubscribes.append(
                await _subscribe(
                    # latest: the caller collects advertisements for a window before it
                    # sends, so its terminal subscriptions are assigned before any
                    # receipt of its own can be published.
                    self._bus,
                    topic,
                    self._terminal(topic),
                    f"{self._group}.t",
                    "latest",
                )
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes.clear()

    async def _on_ad(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
            ad = ModelHostCapacityAdvertisement.model_validate(raw.get("payload", raw))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            AttributeError,
        ):
            return
        self._ads.append(ad)

    def advertisements(self) -> list[ModelHostCapacityAdvertisement]:
        return list(self._ads)

    async def collect(
        self, window_seconds: float
    ) -> list[ModelHostCapacityAdvertisement]:
        """Listen for one window; every live host advertises once per cadence."""
        await asyncio.sleep(window_seconds)
        return self.advertisements()

    def place(
        self,
        *,
        max_load_per_core: float = DEFAULT_MAX_LOAD_PER_CORE,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        need_tools: tuple[str, ...] = (),
        only_host: str | None = None,
    ) -> ModelPlacement:
        return place(
            self._ads,
            evaluated_at=self._now(),
            max_load_per_core=max_load_per_core,
            min_free_bytes=min_free_bytes,
            need_tools=need_tools,
            only_host=only_host,
        )

    def _terminal(self, topic: str) -> Callable[[ProtocolBusMessage], Awaitable[None]]:
        async def on_message(message: ProtocolBusMessage) -> None:
            try:
                raw = json.loads(message.value)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict):
                return
            parent = _uuid_or_none(raw.get("parent_envelope_id"))
            future = self._pending.get(parent) if parent else None
            if future is not None and not future.done():
                future.set_result((topic, raw))

        return on_message

    async def dispatch(
        self, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=request.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(self._topics.command),
        )
        future: asyncio.Future[tuple[str, dict[str, object]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[envelope.envelope_id] = future
        wait = request.timeout_seconds + self._slack
        try:
            await self._bus.publish(
                self._topics.command,
                request.target_host.encode("utf-8"),
                _bytes(envelope),
            )
            topic, raw = await asyncio.wait_for(future, timeout=wait)
        except TimeoutError:
            return _infra_error(
                request,
                f"no terminal for unit {request.work_unit_id} within {wait}s; is "
                f"{request.target_host} serving {self._topics.command}?",
            )
        finally:
            self._pending.pop(envelope.envelope_id, None)
        payload = raw.get("payload")
        if topic == self._topics.failure or not isinstance(payload, dict):
            detail = (
                str(payload.get("error_message", ""))
                if isinstance(payload, dict)
                else "no payload"
            )
            return _infra_error(request, f"pool host failure terminal: {detail}")
        try:
            return ModelLabWorkUnitReceipt.model_validate(payload)
        except ValidationError as exc:
            return _infra_error(request, f"unreadable receipt: {exc}")


def _infra_error(
    request: ModelLabWorkUnitRequest, detail: str
) -> ModelLabWorkUnitReceipt:
    return ModelLabWorkUnitReceipt(
        work_unit_id=request.work_unit_id,
        target_host=request.target_host,
        host="",
        repo=request.repo,
        commit_sha=request.commit_sha,
        lane=request.lane,
        kind=request.kind,
        status=EnumLabWorkUnitStatus.INFRA_ERROR,
        detail=detail[:2000],
    )


__all__ = [
    "DEFAULT_MAX_COMMAND_AGE_SECONDS",
    "DEFAULT_TOOLS",
    "LAB_WORK_NODE",
    "EnumPlacementDecision",
    "LabWorkCaller",
    "LabWorkHost",
    "ModelLabWorkCommandFailure",
    "ModelLabWorkTopics",
    "host_group_id",
    "load_lab_work_topics",
]
