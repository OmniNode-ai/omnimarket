# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fake agent message node: an in-memory topic the command and the MCP tools share."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
    ModelAgentMessageQuery,
)
from omnimarket.nodes.node_agent_message_client_effect.runtime import (
    AgentMessageRuntime,
    fixed_node,
)

FIXED_NOW = datetime(2026, 11, 23, 13, 30, 0, tzinfo=UTC)


class FakeAgentMessageNode:
    def __init__(self) -> None:
        self.topic: list[ModelAgentMessage] = []
        self.sends = 0
        self.reads = 0

    def send(self, message: ModelAgentMessage) -> None:
        self.sends += 1
        self.topic.append(message)

    def read(self, query: ModelAgentMessageQuery) -> tuple[ModelAgentMessage, ...]:
        self.reads += 1
        return tuple(
            m
            for m in self.topic
            if m.to == query.to
            and (query.thread is None or m.thread == query.thread)
            and (query.since is None or m.sent_at >= query.since)
        )


def make_runtime(
    node: FakeAgentMessageNode, environ: dict[str, str] | None = None
) -> AgentMessageRuntime:
    return AgentMessageRuntime(
        open_node=fixed_node(node),
        environ=environ if environ is not None else {},
        clock=lambda: FIXED_NOW,
    )
