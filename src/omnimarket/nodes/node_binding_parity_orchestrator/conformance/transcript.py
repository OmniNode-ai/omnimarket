# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic reference recordings and strict replay bindings for BUS and STO."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
from omnibase_core.protocols.storage.protocol_state_store import ProtocolStateStore
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_bus import (
    run_bus_suite,
)
from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_core import (
    expect,
)
from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_sto import (
    run_sto_suite,
)
from omnimarket.nodes.node_binding_parity_orchestrator.protocols.protocol_local_bus import (
    ProtocolLocalBus,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_bus import (
    MessageCallback,
    build_reference_bus,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_state import (
    StateHarness,
    build_memory_state_harness,
)


class TranscriptMismatchError(AssertionError):
    """A binding call diverged from the recorded operation at a specific step."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelBusDelivery(_FrozenModel):
    """One callback delivery with deterministic broker provenance."""

    topic: str
    group_id: str
    value: str
    offset: str
    partition: int
    retry_count: int


class ModelBusStep(_FrozenModel):
    """An invocation and the callback deliveries caused by it."""

    op: Literal["start", "close", "publish", "subscribe", "unsubscribe"]
    topic: str | None = None
    group_id: str | None = None
    key: str | None = None
    value: str | None = None
    headers: ModelEventHeaders | None = None
    deliveries: list[ModelBusDelivery] = []


class ModelBusTranscript(_FrozenModel):
    """BUS sessions indexed by conformance check name."""

    source: str
    sessions: dict[str, list[ModelBusStep]]


class ModelStateArgs(_FrozenModel):
    """Typed arguments shared by the state protocol operations."""

    node_id: str | None = None
    scope_id: str = "default"
    envelope: ModelStateEnvelope | None = None


class ModelStateStep(_FrozenModel):
    """One state operation, its arguments and its returned value."""

    op: Literal["get", "put", "delete", "exists", "list_keys", "restart"]
    args: ModelStateArgs = ModelStateArgs()
    result: ModelStateEnvelope | bool | list[tuple[str, str]] | None = None


class ModelStateTranscript(_FrozenModel):
    """STO sessions indexed by conformance check name."""

    source: str
    sessions: dict[str, list[ModelStateStep]]


class _Replay[StepT]:
    def __init__(self, steps: list[StepT]) -> None:
        self.steps = steps
        self.index = 0

    def peek(self) -> StepT:
        if self.index == len(self.steps):
            raise TranscriptMismatchError(
                f"step {self.index}: unexpected call after session end"
            )
        return self.steps[self.index]

    def advance(self) -> None:
        self.index += 1

    def mismatch(self, expected: object, actual: object) -> TranscriptMismatchError:
        return TranscriptMismatchError(
            f"step {self.index}: expected {expected!r}, received {actual!r}"
        )

    def assert_complete(self) -> None:
        if self.index != len(self.steps):
            raise TranscriptMismatchError(
                f"step {self.index}: unconsumed recorded steps"
            )


def _session[StepT](sessions: dict[str, list[StepT]], check_name: str) -> list[StepT]:
    if check_name not in sessions:
        raise TranscriptMismatchError(f"step 0: no session for {check_name!r}")
    return sessions[check_name]


class TranscriptBus:
    """Replay every BUS invocation in order, delivering only recorded callbacks."""

    def __init__(self, transcript: ModelBusTranscript, check_name: str) -> None:
        self._replay = _Replay(_session(transcript.sessions, check_name))
        self._subscribers: dict[tuple[str, str], list[MessageCallback]] = {}

    def _take(self, actual: ModelBusStep) -> ModelBusStep:
        expected = self._replay.peek()
        if expected.model_copy(update={"deliveries": []}) != actual:
            raise self._replay.mismatch(expected, actual)
        self._replay.advance()
        return expected

    def assert_complete(self) -> None:
        """Reject a caller that stopped before consuming its session."""
        self._replay.assert_complete()

    async def start(self) -> None:
        self._take(ModelBusStep(op="start"))

    async def close(self) -> None:
        self._take(ModelBusStep(op="close"))
        self.assert_complete()

    async def subscribe(
        self, topic: str, *, on_message: MessageCallback, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        self._take(ModelBusStep(op="subscribe", topic=topic, group_id=group_id))
        callbacks = self._subscribers.setdefault((topic, group_id), [])
        callbacks.append(on_message)

        async def unsubscribe() -> None:
            self._take(ModelBusStep(op="unsubscribe", topic=topic, group_id=group_id))
            callbacks.remove(on_message)

        return unsubscribe

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        step = self._take(
            ModelBusStep(
                op="publish",
                topic=topic,
                key=key.decode("utf-8") if key is not None else None,
                value=value.decode("utf-8"),
                headers=headers,
            )
        )
        positions: dict[tuple[str, str], int] = {}
        for delivery in step.deliveries:
            callbacks = self._subscribers.get((delivery.topic, delivery.group_id), [])
            if not callbacks:
                raise TranscriptMismatchError(
                    f"step {self._replay.index - 1}: delivery has no subscriber"
                )
            message = ModelEventMessage(
                topic=delivery.topic,
                key=key,
                value=delivery.value.encode("utf-8"),
                offset=delivery.offset,
                partition=delivery.partition,
                headers=ModelEventHeaders(
                    source="transcript",
                    event_type=delivery.topic,
                    retry_count=delivery.retry_count,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )
            # Each recorded delivery represents a single callback invocation.
            subscriber = (delivery.topic, delivery.group_id)
            position = positions.get(subscriber, 0)
            await callbacks[position % len(callbacks)](message)
            positions[subscriber] = position + 1


class RecordingBus:
    """Wrap a reference bus and collect its calls and actual callback deliveries."""

    def __init__(self, inner: ProtocolLocalBus, steps: list[ModelBusStep]) -> None:
        self._inner = inner
        self._steps = steps
        self._deliveries: list[ModelBusDelivery] | None = None

    async def start(self) -> None:
        self._steps.append(ModelBusStep(op="start"))
        await self._inner.start()

    async def close(self) -> None:
        self._steps.append(ModelBusStep(op="close"))
        await self._inner.close()

    async def subscribe(
        self, topic: str, *, on_message: MessageCallback, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        self._steps.append(ModelBusStep(op="subscribe", topic=topic, group_id=group_id))

        async def record_delivery(message: ModelEventMessage) -> None:
            if self._deliveries is None or message.offset is None:
                raise TranscriptMismatchError(
                    "delivery outside a recorded publish or without an offset"
                )
            delivery = ModelBusDelivery(
                topic=message.topic,
                group_id=group_id,
                value=message.value.decode("utf-8"),
                offset=str(1000 + int(message.offset)),
                partition=3,
                retry_count=message.headers.retry_count,
            )
            self._deliveries.append(delivery)
            await on_message(
                message.model_copy(
                    update={"offset": delivery.offset, "partition": delivery.partition}
                )
            )

        inner_unsubscribe = await self._inner.subscribe(
            topic, on_message=record_delivery, group_id=group_id
        )

        async def unsubscribe() -> None:
            self._steps.append(
                ModelBusStep(op="unsubscribe", topic=topic, group_id=group_id)
            )
            await inner_unsubscribe()

        return unsubscribe

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        step = ModelBusStep(
            op="publish",
            topic=topic,
            key=key.decode("utf-8") if key is not None else None,
            value=value.decode("utf-8"),
            headers=headers,
        )
        self._steps.append(step)
        previous = self._deliveries
        self._deliveries = step.deliveries
        try:
            await self._inner.publish(topic, key, value, headers)
        finally:
            self._deliveries = previous


class TranscriptStateStore:
    """Replay state calls using a cursor shared across simulated restarts."""

    def __init__(self, transcript: ModelStateTranscript, check_name: str) -> None:
        self._replay = _Replay(_session(transcript.sessions, check_name))

    def _take(
        self,
        op: Literal["get", "put", "delete", "exists", "list_keys", "restart"],
        args: ModelStateArgs,
    ) -> ModelStateStep:
        actual = ModelStateStep(op=op, args=args)
        expected = self._replay.peek()
        if expected.model_copy(update={"result": None}) != actual:
            raise self._replay.mismatch(expected, actual)
        self._replay.advance()
        return expected

    def assert_complete(self) -> None:
        """Reject an unfinished state session."""
        self._replay.assert_complete()

    async def get(
        self, node_id: str, scope_id: str = "default"
    ) -> ModelStateEnvelope | None:
        result = self._take(
            "get", ModelStateArgs(node_id=node_id, scope_id=scope_id)
        ).result
        if result is None or isinstance(result, ModelStateEnvelope):
            return result.model_copy(deep=True) if result is not None else None
        raise TranscriptMismatchError(
            f"step {self._replay.index - 1}: invalid get result"
        )

    async def put(self, envelope: ModelStateEnvelope) -> None:
        self._take("put", ModelStateArgs(envelope=envelope))

    async def delete(self, node_id: str, scope_id: str = "default") -> bool:
        result = self._take(
            "delete", ModelStateArgs(node_id=node_id, scope_id=scope_id)
        ).result
        if isinstance(result, bool):
            return result
        raise TranscriptMismatchError(
            f"step {self._replay.index - 1}: invalid delete result"
        )

    async def exists(self, node_id: str, scope_id: str = "default") -> bool:
        result = self._take(
            "exists", ModelStateArgs(node_id=node_id, scope_id=scope_id)
        ).result
        if isinstance(result, bool):
            return result
        raise TranscriptMismatchError(
            f"step {self._replay.index - 1}: invalid exists result"
        )

    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        result = self._take("list_keys", ModelStateArgs(node_id=node_id)).result
        if isinstance(result, list):
            return list(result)
        raise TranscriptMismatchError(
            f"step {self._replay.index - 1}: invalid list_keys result"
        )

    async def restart(self) -> ProtocolStateStore:
        self._take("restart", ModelStateArgs())
        return copy(self)


def transcript_state_harness(
    transcript: ModelStateTranscript, check_name: str
) -> StateHarness:
    """Build a replay store whose restart consumes the recorded restart operation."""
    store = TranscriptStateStore(transcript, check_name)
    return StateHarness(store, store.restart)


class RecordingStateStore:
    """Record state calls, including the store returned by a simulated restart."""

    def __init__(self, harness: StateHarness, steps: list[ModelStateStep]) -> None:
        self._inner = harness.store
        self._restart = harness.restart
        self._steps = steps

    async def get(
        self, node_id: str, scope_id: str = "default"
    ) -> ModelStateEnvelope | None:
        result = await self._inner.get(node_id, scope_id)
        self._steps.append(
            ModelStateStep(
                op="get",
                args=ModelStateArgs(node_id=node_id, scope_id=scope_id),
                result=result,
            )
        )
        return result

    async def put(self, envelope: ModelStateEnvelope) -> None:
        await self._inner.put(envelope)
        self._steps.append(
            ModelStateStep(
                op="put", args=ModelStateArgs(envelope=envelope.model_copy(deep=True))
            )
        )

    async def delete(self, node_id: str, scope_id: str = "default") -> bool:
        result = await self._inner.delete(node_id, scope_id)
        self._steps.append(
            ModelStateStep(
                op="delete",
                args=ModelStateArgs(node_id=node_id, scope_id=scope_id),
                result=result,
            )
        )
        return result

    async def exists(self, node_id: str, scope_id: str = "default") -> bool:
        result = await self._inner.exists(node_id, scope_id)
        self._steps.append(
            ModelStateStep(
                op="exists",
                args=ModelStateArgs(node_id=node_id, scope_id=scope_id),
                result=result,
            )
        )
        return result

    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        result = await self._inner.list_keys(node_id)
        self._steps.append(
            ModelStateStep(
                op="list_keys",
                args=ModelStateArgs(node_id=node_id),
                result=list(result),
            )
        )
        return result

    async def restart(self) -> ProtocolStateStore:
        restarted = await self._restart()
        self._steps.append(ModelStateStep(op="restart"))
        return RecordingStateStore(StateHarness(restarted, self._restart), self._steps)


def _source(note: str) -> str:
    return f"{note}. Generated by the recorder from the reference binding; not a live capture."


async def record_bus_transcript(source_note: str) -> ModelBusTranscript:
    """Record the BUS suite over the reference bus, with synthetic broker provenance."""
    sessions: dict[str, list[ModelBusStep]] = {}

    def factory(check_name: str) -> ProtocolLocalBus:
        steps: list[ModelBusStep] = []
        sessions[check_name] = steps
        return RecordingBus(build_reference_bus(), steps)

    results = await run_bus_suite(factory)
    expect(
        all(result.passed for result in results),
        f"reference BUS recording failed: {results}",
    )
    return ModelBusTranscript(source=_source(source_note), sessions=sessions)


async def record_state_transcript(source_note: str) -> ModelStateTranscript:
    """Record the STO suite over the reference dictionary persistence binding."""
    sessions: dict[str, list[ModelStateStep]] = {}

    def factory(check_name: str) -> StateHarness:
        steps: list[ModelStateStep] = []
        sessions[check_name] = steps
        recorder = RecordingStateStore(build_memory_state_harness(), steps)
        return StateHarness(recorder, recorder.restart)

    results = await run_sto_suite(factory)
    expect(
        all(result.passed for result in results),
        f"reference STO recording failed: {results}",
    )
    return ModelStateTranscript(source=_source(source_note), sessions=sessions)


def load_bus_transcript(path: Path) -> ModelBusTranscript:
    """Validate and load a BUS transcript from JSON."""
    return ModelBusTranscript.model_validate_json(path.read_text(encoding="utf-8"))


def load_state_transcript(path: Path) -> ModelStateTranscript:
    """Validate and load an STO transcript from JSON."""
    return ModelStateTranscript.model_validate_json(path.read_text(encoding="utf-8"))
