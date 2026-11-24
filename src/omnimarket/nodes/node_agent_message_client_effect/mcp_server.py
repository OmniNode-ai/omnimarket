# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stdio MCP server with exactly two tools, over the same runtime as the command line."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    EnumAgentMessageKind,
    EnumAgentMessagePlatform,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message_request import (
    ModelAgentMessageReadRequest,
    ModelAgentMessageSendRequest,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
)
from omnimarket.nodes.node_agent_message_client_effect.render import (
    message_json,
    render_refusal,
)
from omnimarket.nodes.node_agent_message_client_effect.runtime import (
    AgentMessageRuntime,
)

SEND_TOOL = "send_agent_message"
READ_TOOL = "read_agent_messages"

_KINDS = [k.value for k in EnumAgentMessageKind]
_PLATFORMS = [p.value for p in EnumAgentMessagePlatform]

#: The enums sit in the schema; the handler, not the schema check, refuses a value outside them,
#: so the refusal is the same typed one the command line prints.
_Kind = Annotated[
    str,
    Field(description="Message kind.", json_schema_extra={"enum": _KINDS}),
]
_Platform = Annotated[
    str,
    Field(description="The sender's platform.", json_schema_extra={"enum": _PLATFORMS}),
]


def build_server(runtime: AgentMessageRuntime) -> MCPServer:
    server = MCPServer(
        "agent-message",
        instructions=(
            "Send messages to other agents (a lane name or platform:name) and read the "
            "messages addressed to you, on any platform."
        ),
    )

    @server.tool(
        name=SEND_TOOL,
        description=(
            "Send one message to a lane or platform:name on a thread. The sender defaults "
            "to the caller's declared lane or platform identity."
        ),
    )
    def send_agent_message(
        to: Annotated[
            str, Field(description="Recipient: a lane name or platform:name.")
        ],
        thread: Annotated[str, Field(description="Conversation id.")],
        kind: _Kind,
        platform: _Platform,
        text: Annotated[str, Field(description="Message text.")],
    ) -> dict[str, object]:
        try:
            result = runtime.send(
                ModelAgentMessageSendRequest(
                    to=to, thread=thread, kind=kind, platform=platform, text=text
                )
            )
        except AgentMessageRefusalError as refusal:
            raise ToolError(render_refusal(refusal, as_json=False)) from None
        return {"sent": message_json(result.message)}

    @server.tool(
        name=READ_TOOL,
        description="Read the messages addressed to a recipient, oldest first.",
    )
    def read_agent_messages(
        to: Annotated[str, Field(description="Recipient whose messages to read.")],
        thread: Annotated[
            str | None, Field(description="Only this conversation.")
        ] = None,
        since: Annotated[
            str | None,
            Field(description="Only messages sent at or after this ISO-8601 time."),
        ] = None,
        limit: Annotated[
            int, Field(description="At most this many, newest kept.")
        ] = 50,
    ) -> dict[str, object]:
        try:
            result = runtime.read(
                ModelAgentMessageReadRequest(
                    to=to, thread=thread, since=since, limit=limit
                )
            )
        except AgentMessageRefusalError as refusal:
            raise ToolError(render_refusal(refusal, as_json=False)) from None
        return {"messages": [message_json(m) for m in result.messages]}

    return server
