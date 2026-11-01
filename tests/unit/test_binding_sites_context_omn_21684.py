# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context binding sites preserve explicit dependencies and execute injected fakes."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_infra.runtime.service_terminal_event_consumer import TerminalEventConsumer

from omnimarket.handlers.session_stream_barrier import HandlerSessionStreamBarrier
from omnimarket.nodes.node_context_compaction_orchestrator.container.container_context_compaction_orchestrator import (
    create_handler_compaction_pack_build_bus,
    create_handler_context_compaction_orchestrator,
    create_handler_session_barrier_wait,
    create_handler_session_stream_barrier,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.compaction_runtime_binding import (
    load_compaction_pack_topics,
    load_compaction_runtime_binding,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_compaction_pack_build import (
    HandlerCompactionPackBuildBus,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_context_compaction_orchestrator import (
    HandlerContextCompactionOrchestrator,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_session_barrier_wait import (
    HandlerSessionBarrierWait,
)
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_pack_orchestrator.container.container_context_pack_orchestrator import (
    create_handler_context_pack_builder,
    create_handler_context_pack_orchestrator,
    create_handler_context_pack_render,
    create_handler_context_profile_resolver,
    create_handler_context_task_kind_rules,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    HandlerContextPackRender,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.handlers.handler_context_task_kind_rules import (
    HandlerContextTaskKindRules,
)
from tests.nodes.node_context_compaction_orchestrator.bus_fixture import MetSource
from tests.nodes.node_context_compaction_orchestrator.test_session_barrier_wait import (
    _request,
)
from tests.nodes.node_context_pack_orchestrator.conftest import (
    FIXED_IDENTITY,
    command,
    sources,
)


def _publish(topic: str, payload: bytes) -> None:
    """Shape of the event publisher callable the compaction orchestrator takes."""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory", "handler_class"),
    [
        (create_handler_context_pack_builder, HandlerContextPackBuilder),
        (create_handler_context_pack_render, HandlerContextPackRender),
        (create_handler_context_task_kind_rules, HandlerContextTaskKindRules),
        (create_handler_session_stream_barrier, HandlerSessionStreamBarrier),
    ],
)
def test_stateless_factories(
    factory: Callable[[], object], handler_class: type[object]
) -> None:
    assert isinstance(factory(), handler_class)


@pytest.mark.unit
def test_profile_resolver_factory_wires_directory(tmp_path: Path) -> None:
    resolver = create_handler_context_profile_resolver(tmp_path)
    assert isinstance(resolver, HandlerContextProfileResolver)
    assert resolver._profiles_dir is tmp_path


@pytest.mark.unit
def test_pack_factory_wires_every_dependency() -> None:
    resolver = create_handler_context_profile_resolver()
    builder = create_handler_context_pack_builder()
    renderer = create_handler_context_pack_render()
    rules = create_handler_context_task_kind_rules()
    bound = sources()
    clock = time.perf_counter
    handler = create_handler_context_pack_orchestrator(
        bound,
        profile_resolver=resolver,
        builder=builder,
        renderer=renderer,
        task_kind_rules=rules,
        clock=clock,
    )
    assert isinstance(handler, HandlerContextPackOrchestrator)
    assert handler._resolver is resolver
    assert handler._builder is builder
    assert handler._renderer is renderer
    assert handler._task_kind_rules is rules
    assert handler._clock is clock
    for factor, source in bound.items():
        assert handler._sources[factor] is source


@pytest.mark.unit
@pytest.mark.parametrize(
    ("parameter", "attribute"),
    [
        ("profile_resolver", "_resolver"),
        ("builder", "_builder"),
        ("renderer", "_renderer"),
        ("task_kind_rules", "_task_kind_rules"),
    ],
)
def test_pack_handler_executes_explicit_falsey_fake(
    parameter: str, attribute: str
) -> None:
    fake = MagicMock()
    fake.__bool__.return_value = False
    fake.handle.side_effect = RuntimeError
    dependencies: dict[str, Any] = {parameter: fake}
    handler = create_handler_context_pack_orchestrator(
        sources(), implementation_identity=lambda: FIXED_IDENTITY, **dependencies
    )
    assert getattr(handler, attribute) is fake
    with pytest.raises(RuntimeError):
        handler.handle(command())
    fake.handle.assert_called_once()


@pytest.mark.unit
def test_wait_factory_wires_dependencies_and_executes() -> None:
    binding = load_compaction_runtime_binding()
    source = MetSource()
    monotonic = time.monotonic
    sleep = MagicMock()
    handler = create_handler_session_barrier_wait(
        source,
        deadline_s=binding.barrier_deadline_s,
        poll_interval_s=binding.barrier_poll_interval_s,
        monotonic=monotonic,
        sleep=sleep,
    )
    assert isinstance(handler, HandlerSessionBarrierWait)
    assert handler._probe_source is source
    assert isinstance(handler._barrier, HandlerSessionStreamBarrier)
    assert handler._monotonic is monotonic
    assert handler._sleep is sleep
    assert handler._deadline_s == binding.barrier_deadline_s
    assert handler._poll_interval_s == binding.barrier_poll_interval_s
    assert handler.wait(_request()).outcome == "met"
    sleep.assert_not_called()


@pytest.mark.unit
def test_bus_factory_wires_transport_and_topics() -> None:
    topics = load_compaction_pack_topics()
    publisher = MagicMock(spec=_publish)
    consumer = MagicMock(spec=TerminalEventConsumer)
    access = ModelContextAccess(tenant_id=command().access.tenant_id)
    monotonic = time.monotonic
    handler = create_handler_compaction_pack_build_bus(
        publisher,
        consumer,
        access,
        topics.request_topic,
        topics.completed_topic,
        topics.failed_topic,
        monotonic=monotonic,
    )
    assert isinstance(handler, HandlerCompactionPackBuildBus)
    assert handler._publisher is publisher
    assert handler._consumer is consumer
    assert handler._access is access
    assert handler._request_topic == topics.request_topic
    assert handler._completed_topic == topics.completed_topic
    assert handler._failed_topic == topics.failed_topic
    assert handler._monotonic is monotonic
    publisher.side_effect = RuntimeError
    deadline = time.monotonic() + load_compaction_runtime_binding().barrier_deadline_s
    with pytest.raises(RuntimeError):
        handler.build(_request(), source_revision=None, deadline=deadline)
    consumer.open.assert_any_call(topics.completed_topic)
    consumer.open.assert_any_call(topics.failed_topic)
    publisher.assert_called_once()
    assert publisher.call_args.args[0] == topics.request_topic


@pytest.mark.unit
def test_compaction_factory_wires_and_executes_explicit_fakes() -> None:
    binding = load_compaction_runtime_binding()
    source = MetSource()
    pack_build = MagicMock(spec=HandlerCompactionPackBuildBus)
    barrier = MagicMock(spec=HandlerSessionStreamBarrier)
    barrier.handle.return_value.outcome = "met"
    barrier_wait = MagicMock(spec=HandlerSessionBarrierWait)
    barrier_wait.wait.return_value.outcome = "met"
    handler = create_handler_context_compaction_orchestrator(
        binding=binding,
        barrier_probe_source=source,
        pack_build=pack_build,
        stream_barrier=barrier,
        barrier_wait=barrier_wait,
    )
    assert isinstance(handler, HandlerContextCompactionOrchestrator)
    assert handler._pack_build is pack_build
    assert handler._probe_source is source
    assert handler._stream_barrier is barrier
    assert handler._barrier_wait is barrier_wait
    request = _request()
    assert handler._barrier_failure(request) is None
    barrier_wait.wait.assert_called_once_with(request)
    deadline = time.monotonic() + binding.barrier_deadline_s
    assert handler._build(request, deadline) is pack_build.build.return_value
    assert request.barrier_id is not None
    probe = source.read(request.session_id, request.barrier_id)
    barrier.handle.assert_called_once_with(probe)
    pack_build.build.assert_called_once_with(
        request,
        source_revision=probe.source_revision,
        deadline=deadline,
        lane=probe.lane,
    )


@pytest.mark.unit
def test_compaction_factory_builds_default_runtime_dependencies() -> None:
    binding = load_compaction_runtime_binding().model_copy(
        update={"tenant_id": command().access.tenant_id}
    )
    publisher = MagicMock(spec=_publish)
    consumer = MagicMock(spec=TerminalEventConsumer)
    source = MetSource()
    handler = create_handler_context_compaction_orchestrator(
        publisher, consumer, binding=binding, barrier_probe_source=source
    )
    assert isinstance(handler._pack_build, HandlerCompactionPackBuildBus)
    assert handler._pack_build._publisher is publisher
    assert handler._pack_build._consumer is consumer
    assert handler._probe_source is source
    wait = cast(HandlerSessionBarrierWait, handler._barrier_wait)
    assert isinstance(wait, HandlerSessionBarrierWait)
    assert wait._probe_source is source
    assert isinstance(wait._barrier, HandlerSessionStreamBarrier)
    assert isinstance(handler._stream_barrier, HandlerSessionStreamBarrier)
