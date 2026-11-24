# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open the declared bus, drive the agent message node on it, and give the handlers a
synchronous port.

The topic and its I/O belong to the agent message node. This module only opens the broker the
caller declared (``--bus-lane`` or ``--kafka-bootstrap``), keeps one event loop on a thread for
the life of the command, and turns the node's awaited calls into the port the handlers use.
A missing or refused broker is a typed refusal, never a stack trace.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
    ModelAgentMessageQuery,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
    EnumAgentMessageRefusal,
)
from omnimarket.nodes.node_agent_message_client_effect.ports.protocol_agent_message_bus_node import (
    ProtocolAgentMessageBusNode,
)
from omnimarket.nodes.node_agent_message_client_effect.ports.protocol_agent_message_node import (
    ProtocolAgentMessageNode,
)
from omnimarket.nodes.node_agent_message_client_effect.runtime import (
    AgentMessageRuntime,
    BusSettings,
)

NodeFactory = Callable[
    [ProtocolLabRunBus], AbstractAsyncContextManager[ProtocolAgentMessageBusNode]
]

open_bus = positional_opener(open_lab_run_bus)
_READY_TIMEOUT_SECONDS = 120
_CALL_TIMEOUT_SECONDS = 120


class _LoopThreadNode:
    """The synchronous port: each call is run on the loop that holds the open bus."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, node: ProtocolAgentMessageBusNode
    ) -> None:
        self._loop = loop
        self._node = node

    def send(self, message: ModelAgentMessage) -> None:
        future = asyncio.run_coroutine_threadsafe(self._node.send(message), self._loop)
        future.result(timeout=_CALL_TIMEOUT_SECONDS)

    def read(self, query: ModelAgentMessageQuery) -> tuple[ModelAgentMessage, ...]:
        future = asyncio.run_coroutine_threadsafe(self._node.read(query), self._loop)
        return future.result(timeout=_CALL_TIMEOUT_SECONDS)


def check_broker_declared(settings: BusSettings) -> None:
    if settings.bus == "kafka" and not (settings.bus_lane or settings.kafka_bootstrap):
        raise AgentMessageRefusalError(
            EnumAgentMessageRefusal.UNDECLARED_BROKER,
            "no broker is declared: pass --bus-lane <lane> (or "
            "$ONEX_AGENT_MESSAGE_BUS_LANE) or --kafka-bootstrap, or use --bus inmemory",
        )


@contextlib.contextmanager
def open_bus_node(
    settings: BusSettings, node_factory: NodeFactory
) -> Iterator[ProtocolAgentMessageNode]:
    """Open the declared bus and the node on it; close both on exit."""
    check_broker_declared(settings)
    bus_kind: BusKind = "inmemory" if settings.bus == "inmemory" else "kafka"
    loop_box: list[asyncio.AbstractEventLoop] = []
    node_box: list[ProtocolAgentMessageBusNode] = []
    errors: list[BaseException] = []
    ready = threading.Event()
    stop: list[asyncio.Event] = []

    async def main() -> None:
        loop_box.append(asyncio.get_running_loop())
        stop.append(asyncio.Event())
        async with (
            open_bus(
                bus_kind,
                settings.bus_lane,
                settings.kafka_bootstrap,
                settings.omnibase_path,
            ) as bus,
            node_factory(bus) as node,
        ):
            node_box.append(node)
            ready.set()
            await stop[0].wait()

    def thread_main() -> None:
        try:
            asyncio.run(main())
        except BaseException as exc:
            # Handed to the calling thread, which raises it after the ready wait.
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=thread_main, name="agent-message-bus", daemon=True)
    thread.start()
    if not ready.wait(_READY_TIMEOUT_SECONDS):
        raise AgentMessageRefusalError(
            EnumAgentMessageRefusal.BROKER_REFUSED,
            f"the bus was not ready within {_READY_TIMEOUT_SECONDS}s",
        )
    if errors:
        error = errors[0]
        if isinstance(error, LabRunBusError):
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.BROKER_REFUSED, str(error)
            ) from None
        raise error
    try:
        yield _LoopThreadNode(loop_box[0], node_box[0])
    finally:
        loop_box[0].call_soon_threadsafe(stop[0].set)
        thread.join(timeout=60)


def agent_message_node_factory() -> NodeFactory:
    """The agent message node's factory: the one place this client meets that node.

    The topic, the message model and the node that publishes and reads belong to the agent
    message node, which is not in this runtime yet; until it is, the client refuses with a
    typed error instead of writing to a topic itself.
    """
    raise AgentMessageRefusalError(
        EnumAgentMessageRefusal.NODE_UNAVAILABLE,
        "the agent message node is not installed in this runtime",
    )


def bus_runtime(
    settings: BusSettings,
    node_factory: Callable[[], NodeFactory] = agent_message_node_factory,
) -> AgentMessageRuntime:
    def opener() -> AbstractContextManager[ProtocolAgentMessageNode]:
        check_broker_declared(settings)
        return open_bus_node(settings, node_factory())

    return AgentMessageRuntime(open_node=opener)
