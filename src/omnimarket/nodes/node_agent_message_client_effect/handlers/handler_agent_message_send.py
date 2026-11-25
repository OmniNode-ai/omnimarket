# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check a send request, stamp it, and hand it to the agent message node."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    EnumAgentMessageKind,
    EnumAgentMessagePlatform,
    ModelAgentMessage,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message_request import (
    ModelAgentMessageSendRequest,
    ModelAgentMessageSendResult,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
    EnumAgentMessageRefusal,
)
from omnimarket.nodes.node_agent_message_client_effect.ports.protocol_agent_message_node import (
    ProtocolAgentMessageNode,
)

_PLATFORM_PREFIX_SEPARATOR = ":"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_message_id() -> str:
    return str(uuid.uuid4())


def check_recipient(to: str) -> str:
    """Return the recipient, or refuse a blank one or a ``platform:name`` with an unknown platform."""
    recipient = to.strip()
    if not recipient:
        raise AgentMessageRefusalError(
            EnumAgentMessageRefusal.MISSING_RECIPIENT,
            "--to names a lane or platform:name; none was given",
        )
    prefix, separator, name = recipient.partition(_PLATFORM_PREFIX_SEPARATOR)
    if separator:
        if not name:
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.MISSING_RECIPIENT,
                f"{recipient!r} has no name after the platform",
            )
        if prefix not in {p.value for p in EnumAgentMessagePlatform}:
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.UNKNOWN_PLATFORM,
                f"{prefix!r} is not a platform; one of {platform_names()}",
            )
    return recipient


def platform_names() -> str:
    return ", ".join(p.value for p in EnumAgentMessagePlatform)


def kind_names() -> str:
    return ", ".join(k.value for k in EnumAgentMessageKind)


class HandlerAgentMessageSend:
    """Send one message; every refusal is raised before the node is called."""

    def __init__(
        self,
        node: ProtocolAgentMessageNode,
        *,
        clock: Callable[[], datetime] = _utc_now,
        new_message_id: Callable[[], str] = _new_message_id,
    ) -> None:
        self._node = node
        self._clock = clock
        self._new_message_id = new_message_id

    def handle(
        self, request: ModelAgentMessageSendRequest
    ) -> ModelAgentMessageSendResult:
        to = check_recipient(request.to)
        thread = request.thread.strip()
        if not thread:
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.MISSING_THREAD,
                "--thread names the conversation; none was given",
            )
        try:
            kind = EnumAgentMessageKind(request.kind.strip())
        except ValueError:
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.UNKNOWN_KIND,
                f"{request.kind!r} is not a kind; one of {kind_names()}",
            ) from None
        try:
            platform = EnumAgentMessagePlatform(request.platform.strip())
        except ValueError:
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.UNKNOWN_PLATFORM,
                f"{request.platform!r} is not a platform; one of {platform_names()}",
            ) from None
        if not request.text.strip():
            raise AgentMessageRefusalError(
                EnumAgentMessageRefusal.EMPTY_TEXT, "the message text is empty"
            )
        sender = (request.sender or "").strip() or (
            f"{platform.value}{_PLATFORM_PREFIX_SEPARATOR}{platform.value}"
        )
        message = ModelAgentMessage(
            message_id=self._new_message_id(),
            sender=sender,
            to=to,
            thread=thread,
            kind=kind,
            platform=platform,
            text=request.text,
            sent_at=self._clock().astimezone(UTC),
        )
        event_id = self._node.send(message)
        if event_id:
            message = message.model_copy(update={"message_id": event_id})
        return ModelAgentMessageSendResult(message=message)
