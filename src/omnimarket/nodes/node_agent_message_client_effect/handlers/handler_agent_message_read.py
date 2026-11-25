# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check a read request and ask the agent message node for the messages."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_agent_message_client_effect.handlers.handler_agent_message_send import (
    check_recipient,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessageQuery,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message_request import (
    ModelAgentMessageReadRequest,
    ModelAgentMessageReadResult,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
    EnumAgentMessageRefusal,
)
from omnimarket.nodes.node_agent_message_client_effect.ports.protocol_agent_message_node import (
    ProtocolAgentMessageNode,
)

_MAX_LIMIT = 1000


def parse_since(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp; a value with no zone is read as UTC."""
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise AgentMessageRefusalError(
            EnumAgentMessageRefusal.BAD_SINCE,
            f"{raw!r} is not an ISO-8601 timestamp such as 2026-11-23T13:00:00Z",
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class HandlerAgentMessageRead:
    """Read the messages addressed to one recipient, oldest first."""

    def __init__(self, node: ProtocolAgentMessageNode) -> None:
        self._node = node

    def handle(
        self, request: ModelAgentMessageReadRequest
    ) -> ModelAgentMessageReadResult:
        to = check_recipient(request.to)
        if not 1 <= request.limit <= _MAX_LIMIT:
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.BAD_LIMIT,
                f"--limit is between 1 and {_MAX_LIMIT}; got {request.limit}",
            )
        thread = (request.thread or "").strip() or None
        since = parse_since(request.since) if request.since else None
        report = self._node.read(
            ModelAgentMessageQuery(
                to=to, thread=thread, since=since, limit=request.limit
            )
        )
        ordered = tuple(sorted(report.messages, key=lambda m: m.sent_at))
        return ModelAgentMessageReadResult(
            messages=ordered[-request.limit :],
            unverified=report.unverified,
            rejections=report.rejections,
        )
