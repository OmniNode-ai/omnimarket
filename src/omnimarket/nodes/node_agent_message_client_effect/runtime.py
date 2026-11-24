# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the command and the MCP server reach outside themselves with.

Both entry points build the same :class:`AgentMessageRuntime` and call the same two handlers,
so a send or a read behaves the same on the command line and as an MCP tool.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_agent_message_client_effect.handlers.handler_agent_message_read import (
    HandlerAgentMessageRead,
)
from omnimarket.nodes.node_agent_message_client_effect.handlers.handler_agent_message_send import (
    HandlerAgentMessageSend,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
    ModelAgentMessageQuery,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message_request import (
    ModelAgentMessageReadRequest,
    ModelAgentMessageReadResult,
    ModelAgentMessageSendRequest,
    ModelAgentMessageSendResult,
)
from omnimarket.nodes.node_agent_message_client_effect.ports.protocol_agent_message_node import (
    ProtocolAgentMessageNode,
)

#: The lane the caller declared (a lane's own name); read as the default sender.
LANE_ENV = "ONEX_LANE"
#: A platform session with no lane names itself here, e.g. ``codex:review-7``.
AGENT_IDENTITY_ENV = "ONEX_AGENT_IDENTITY"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def caller_identity(environ: Mapping[str, str]) -> str | None:
    """The caller's declared lane or platform identity, or None when it declared none."""
    for name in (LANE_ENV, AGENT_IDENTITY_ENV):
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class BusSettings:
    """Where the broker is declared: the same options every bus-backed onex command takes."""

    bus: str = "kafka"
    bus_lane: str | None = None
    kafka_bootstrap: str | None = None
    omnibase_path: Path | None = None


NodeOpener = Callable[[], AbstractContextManager[ProtocolAgentMessageNode]]


class _LazyNode:
    """Opens the node on its first call, so a refused request never opens the broker."""

    def __init__(self, opener: NodeOpener) -> None:
        self._opener = opener
        self._stack = contextlib.ExitStack()
        self._node: ProtocolAgentMessageNode | None = None

    def __enter__(self) -> _LazyNode:
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def _open(self) -> ProtocolAgentMessageNode:
        if self._node is None:
            self._node = self._stack.enter_context(self._opener())
        return self._node

    def send(self, message: ModelAgentMessage) -> None:
        self._open().send(message)

    def read(self, query: ModelAgentMessageQuery) -> tuple[ModelAgentMessage, ...]:
        return self._open().read(query)


@dataclass(frozen=True)
class AgentMessageRuntime:
    """An opener for the node, the caller's environment and a clock."""

    open_node: NodeOpener
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    clock: Callable[[], datetime] = _utc_now

    def send(
        self, request: ModelAgentMessageSendRequest
    ) -> ModelAgentMessageSendResult:
        stamped = request.model_copy(
            update={"sender": request.sender or caller_identity(self.environ)}
        )
        with self.open_node() as node:
            return HandlerAgentMessageSend(node, clock=self.clock).handle(stamped)

    def read(
        self, request: ModelAgentMessageReadRequest
    ) -> ModelAgentMessageReadResult:
        with self.open_node() as node:
            return HandlerAgentMessageRead(node).handle(request)


def fixed_node(node: ProtocolAgentMessageNode) -> NodeOpener:
    """An opener over one already-built node; used by tests and by in-process callers."""

    @contextlib.contextmanager
    def opener() -> Iterator[ProtocolAgentMessageNode]:
        yield node

    return opener
