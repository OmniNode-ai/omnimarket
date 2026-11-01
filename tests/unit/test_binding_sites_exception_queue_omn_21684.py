# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exception queue bindings preserve explicit dependencies and route work to them."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.model_host_pool_reading import ModelHostPoolReading
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_cli,
    create_handler_exception_emit,
    create_handler_exception_queue,
    create_handler_exception_queue_read,
    create_handler_exception_resolve,
    create_node_exception_queue_effect,
)
from omnimarket.nodes.node_exception_queue_effect.handler import (
    AUDIENCES,
    HandlerExceptionCli,
    HandlerExceptionEmit,
    HandlerExceptionQueue,
    HandlerExceptionQueueRead,
    HandlerExceptionResolve,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionCliRequest,
    ModelExceptionEmitRequest,
    ModelExceptionEmitResult,
    ModelExceptionQueueReadRequest,
    ModelExceptionQueueRequest,
    ModelExceptionQueueResult,
    ModelExceptionResolveRequest,
    ModelExceptionResolveResult,
)
from omnimarket.nodes.node_exception_queue_effect.node import NodeExceptionQueueEffect
from omnimarket.nodes.node_host_pool_alarm_effect.container.container_host_pool_alarm_effect import (
    create_handler_host_pool_alarm,
    create_node_host_pool_alarm_effect,
)
from omnimarket.nodes.node_host_pool_alarm_effect.handler import (
    CLASS_SWAPPING,
    CONSECUTIVE,
    SOURCE,
    TICKET,
    HandlerHostPoolAlarm,
)
from omnimarket.nodes.node_host_pool_alarm_effect.models import (
    SWAP_IN_BOUND_KB_S,
    ModelHostPoolAlarmRequest,
)
from omnimarket.nodes.node_host_pool_alarm_effect.node import NodeHostPoolAlarmEffect


class FakeEmit(HandlerExceptionEmit):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelExceptionEmitRequest] = []

    def __bool__(self) -> bool:
        return False

    def handle(self, request: ModelExceptionEmitRequest) -> ModelExceptionEmitResult:
        self.requests.append(request)
        return ModelExceptionEmitResult("appended", TICKET, 1, "", "")


class FakeQueue(HandlerExceptionQueue):
    def __init__(self) -> None:
        self.requests: list[ModelExceptionQueueRequest] = []
        self.result = ModelExceptionQueueResult([], [])

    def __bool__(self) -> bool:
        return False

    def handle(self, request: ModelExceptionQueueRequest) -> ModelExceptionQueueResult:
        self.requests.append(request)
        return self.result


class FakeResolve(HandlerExceptionResolve):
    def __init__(self) -> None:
        self.requests: list[ModelExceptionResolveRequest] = []

    def __bool__(self) -> bool:
        return False

    def handle(
        self, request: ModelExceptionResolveRequest
    ) -> ModelExceptionResolveResult:
        self.requests.append(request)
        return ModelExceptionResolveResult("resolved", "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (create_handler_exception_emit, HandlerExceptionEmit),
        (create_handler_exception_queue, HandlerExceptionQueue),
        (create_handler_exception_resolve, HandlerExceptionResolve),
        (create_node_exception_queue_effect, NodeExceptionQueueEffect),
        (create_node_host_pool_alarm_effect, NodeHostPoolAlarmEffect),
    ],
)
def test_leaf_factories_return_the_declared_class(
    factory: Callable[[], object], expected: type[object]
) -> None:
    assert isinstance(factory(), expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (create_handler_exception_cli, HandlerExceptionCli),
        (create_handler_host_pool_alarm, HandlerHostPoolAlarm),
    ],
)
def test_composite_factories_wire_explicit_dependencies(
    factory: Callable[..., HandlerExceptionCli | HandlerHostPoolAlarm],
    expected: type[object],
) -> None:
    emit, queue, resolve = FakeEmit(), FakeQueue(), FakeResolve()
    handler = factory(emit=emit, queue=queue, resolve=resolve)
    assert isinstance(handler, expected)
    assert handler._emit is emit
    assert handler._queue is queue
    assert handler._resolve is resolve


@pytest.mark.unit
def test_queue_read_factory_wires_and_uses_the_explicit_queue(tmp_path: Path) -> None:
    queue = FakeQueue()
    reader = create_handler_exception_queue_read(queue=queue)
    assert isinstance(reader, HandlerExceptionQueueRead)
    assert reader._queue is queue

    result = reader.handle(ModelExceptionQueueReadRequest(tmp_path))

    assert not result.reasons
    assert queue.requests == [
        ModelExceptionQueueRequest(tmp_path, a) for a in AUDIENCES
    ]


@pytest.mark.unit
def test_composite_factories_build_default_dependencies() -> None:
    reader = create_handler_exception_queue_read()
    assert isinstance(reader._queue, HandlerExceptionQueue)
    for handler in (create_handler_exception_cli(), create_handler_host_pool_alarm()):
        assert isinstance(handler._emit, HandlerExceptionEmit)
        assert isinstance(handler._queue, HandlerExceptionQueue)
        assert isinstance(handler._resolve, HandlerExceptionResolve)


@pytest.mark.unit
@pytest.mark.parametrize("verb", ["emit", "list", "resolve"])
def test_cli_uses_the_explicit_dependency(tmp_path: Path, verb: str) -> None:
    emit, queue, resolve = FakeEmit(), FakeQueue(), FakeResolve()
    handler = create_handler_exception_cli(emit=emit, queue=queue, resolve=resolve)

    result = handler.handle(
        ModelExceptionCliRequest([verb, "--state-dir", str(tmp_path), "--no-dispatch"])
    )

    assert result.code == 0
    assert bool(emit.requests) == (verb == "emit")
    assert bool(queue.requests) == (verb == "list")
    assert bool(resolve.requests) == (verb == "resolve")
    for request in (*emit.requests, *queue.requests, *resolve.requests):
        assert request.state_dir == tmp_path


@pytest.mark.unit
def test_host_alarm_uses_injected_emit_queue_and_resolve(tmp_path: Path) -> None:
    emit, queue, resolve = FakeEmit(), FakeQueue(), FakeResolve()
    handler = create_handler_host_pool_alarm(emit=emit, queue=queue, resolve=resolve)
    now = datetime.now(UTC)
    swapping = ModelHostPoolReading(
        SOURCE, CONSECUTIVE, None, swap_in_kb_s=SWAP_IN_BOUND_KB_S + 1
    )
    request = ModelHostPoolAlarmRequest(
        [swapping], now, tmp_path, exception_dir=tmp_path, plugin_repo=SOURCE
    )

    result = handler.handle(request)

    assert SOURCE in result.silenced
    (emitted,) = emit.requests
    assert emitted.state_dir == tmp_path
    assert emitted.row.cls == CLASS_SWAPPING
    assert emitted.row.key == SOURCE
    queue.result = ModelExceptionQueueResult(
        [{"key": emitted.row.key, "cls": emitted.row.cls, "id": TICKET}], []
    )
    clean = ModelHostPoolAlarmRequest(
        [ModelHostPoolReading(SOURCE, CONSECUTIVE, None)],
        now,
        tmp_path,
        exception_dir=tmp_path,
        plugin_repo=SOURCE,
    )
    for _ in range(CONSECUTIVE):
        handler.handle(clean)

    assert len(emit.requests) == 1
    assert queue.requests == [ModelExceptionQueueRequest(tmp_path, AUDIENCES[0])]
    (resolved,) = resolve.requests
    assert resolved.state_dir == tmp_path
    assert resolved.id == TICKET
