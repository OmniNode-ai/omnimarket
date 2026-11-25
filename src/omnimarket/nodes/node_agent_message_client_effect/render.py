# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plain-text and JSON forms of what the client returns."""

from __future__ import annotations

import json

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message_request import (
    ModelAgentMessageReadResult,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
)


def message_json(message: ModelAgentMessage) -> dict[str, object]:
    return message.model_dump(mode="json", by_alias=True)


def message_text(message: ModelAgentMessage) -> str:
    header = (
        f"[{message.sent_at.isoformat()}] {message.sender} -> {message.to} "
        f"({message.kind.value}, {message.platform.value}) "
        f"thread={message.thread} id={message.message_id}"
    )
    body = "\n".join(f"  {line}" for line in message.text.splitlines())
    return f"{header}\n{body}"


def render_sent(message: ModelAgentMessage, *, as_json: bool) -> str:
    if as_json:
        return json.dumps({"sent": message_json(message)}, sort_keys=True)
    return f"sent {message.message_id} to {message.to} on thread {message.thread}"


def read_json(result: ModelAgentMessageReadResult) -> dict[str, object]:
    """The read answer: the accepted messages, the legacy count and every refused record with its typed reason."""
    return {
        "messages": [message_json(m) for m in result.messages],
        "unverified": result.unverified,
        "rejections": [r.model_dump(mode="json") for r in result.rejections],
    }


def render_read(result: ModelAgentMessageReadResult, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(read_json(result), sort_keys=True)
    lines = [message_text(m) for m in result.messages] or ["no messages"]
    if result.unverified:
        lines.append(
            f"{result.unverified} message(s) carry no sender proof (legacy shape)"
        )
    lines.extend(
        f"refused: {r.reason.value}: sender={r.sender or '-'} id={r.message_id or '-'}"
        for r in result.rejections
    )
    return "\n".join(lines)


def render_refusal(refusal: AgentMessageRefusalError, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {"refused": refusal.code.value, "detail": refusal.detail}, sort_keys=True
        )
    return f"refused: {refusal.code.value}: {refusal.detail}"
