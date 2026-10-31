# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed construction of the compaction orchestrator and its handlers."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from omnibase_core.models.context.model_context_access import ModelContextAccess

from omnimarket.handlers.session_stream_barrier import HandlerSessionStreamBarrier
from omnimarket.nodes.node_context_compaction_orchestrator.container.container_barrier import (
    create_barrier,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.compaction_runtime_binding import (
    ModelCompactionRuntimeBinding,
    bind_compaction_ports,
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
from omnimarket.protocols.protocol_context_compaction_barrier_wait import (
    ProtocolContextCompactionBarrierWait,
)
from omnimarket.protocols.protocol_session_barrier_probe_source import (
    ProtocolSessionBarrierProbeSource,
)

if TYPE_CHECKING:
    from omnibase_infra.runtime.service_terminal_event_consumer import (
        TerminalEventConsumer,
    )


def create_handler_session_stream_barrier() -> HandlerSessionStreamBarrier:
    """Build the stateless stream barrier evaluator."""
    return create_barrier()


def create_handler_session_barrier_wait(
    probe_source: ProtocolSessionBarrierProbeSource,
    *,
    deadline_s: float,
    poll_interval_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HandlerSessionBarrierWait:
    """Wire the projection reader, barrier evaluator and polling clock."""
    return HandlerSessionBarrierWait(
        probe_source,
        deadline_s=deadline_s,
        poll_interval_s=poll_interval_s,
        monotonic=monotonic,
        sleep=sleep,
    )


def create_handler_compaction_pack_build_bus(
    event_publisher: Callable[[str, bytes], None],
    event_consumer: TerminalEventConsumer,
    access: ModelContextAccess,
    request_topic: str,
    completed_topic: str,
    failed_topic: str,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> HandlerCompactionPackBuildBus:
    """Wire the bus transport and contract-declared pack topics."""
    return HandlerCompactionPackBuildBus(
        event_publisher,
        event_consumer,
        access,
        request_topic,
        completed_topic,
        failed_topic,
        monotonic=monotonic,
    )


def create_handler_context_compaction_orchestrator(
    event_publisher: Callable[[str, bytes], None] | None = None,
    event_consumer: TerminalEventConsumer | None = None,
    *,
    activation_ids: Callable[[], UUID] = uuid4,
    barrier_wait: ProtocolContextCompactionBarrierWait | None = None,
    clock: Callable[[], float] | None = None,
    barrier_probe_source: ProtocolSessionBarrierProbeSource | None = None,
    binding: ModelCompactionRuntimeBinding | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    pack_build: HandlerCompactionPackBuildBus | None = None,
    stream_barrier: HandlerSessionStreamBarrier | None = None,
) -> HandlerContextCompactionOrchestrator:
    """Wire runtime ports when a bus transport and tenant are available."""
    binding = binding if binding is not None else load_compaction_runtime_binding()
    if (
        pack_build is None
        and event_publisher is not None
        and event_consumer is not None
        and binding.tenant_id is not None
    ):
        topics = load_compaction_pack_topics()
        bound_wait, barrier_probe_source = bind_compaction_ports(
            binding, barrier_probe_source=barrier_probe_source, monotonic=monotonic
        )
        if barrier_wait is None:
            barrier_wait = bound_wait
        pack_build = create_handler_compaction_pack_build_bus(
            event_publisher,
            event_consumer,
            ModelContextAccess(tenant_id=binding.tenant_id),
            topics.request_topic,
            topics.completed_topic,
            topics.failed_topic,
            monotonic=monotonic,
        )
    return HandlerContextCompactionOrchestrator(
        event_publisher,
        event_consumer,
        activation_ids=activation_ids,
        barrier_wait=barrier_wait,
        clock=clock,
        barrier_probe_source=barrier_probe_source,
        binding=binding,
        monotonic=monotonic,
        pack_build=pack_build,
        stream_barrier=(
            stream_barrier
            if stream_barrier is not None
            else create_handler_session_stream_barrier()
        ),
    )


__all__ = [
    "create_handler_compaction_pack_build_bus",
    "create_handler_context_compaction_orchestrator",
    "create_handler_session_barrier_wait",
    "create_handler_session_stream_barrier",
]
