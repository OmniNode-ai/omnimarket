# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent message MCP server: two tools, each reaching the same node as the command line."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import ToolError

from omnimarket.nodes.node_agent_message_client_effect.cli import agent_message_group
from omnimarket.nodes.node_agent_message_client_effect.mcp_server import (
    READ_TOOL,
    SEND_TOOL,
    build_server,
)
from tests.cli.agent_message_fakes import FakeAgentMessageNode, make_runtime

pytestmark = pytest.mark.unit

_SEND_ARGS: dict[str, Any] = {
    "to": "codex:review",
    "thread": "t-9",
    "kind": "question",
    "platform": "claude",
    "text": "hello",
}


def test_lists_exactly_the_send_and_read_tools_with_closed_enums() -> None:
    server = build_server(make_runtime(FakeAgentMessageNode()))
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == sorted([SEND_TOOL, READ_TOOL])
    send = next(t for t in tools if t.name == SEND_TOOL)
    props = send.input_schema["properties"]
    assert set(props) == {"to", "thread", "kind", "platform", "text"}
    assert props["kind"]["enum"] == ["message", "question", "answer", "notice"]
    assert props["platform"]["enum"] == [
        "claude",
        "codex",
        "glm",
        "hermes",
        "gemini",
        "other",
    ]
    read = next(t for t in tools if t.name == READ_TOOL)
    assert set(read.input_schema["properties"]) == {"to", "thread", "since", "limit"}


def test_tools_reach_the_same_node_as_the_command_line() -> None:
    node = FakeAgentMessageNode()
    runtime = make_runtime(node, {"ONEX_LANE": "mcp-caller"})
    server = build_server(runtime)

    asyncio.run(server.call_tool(SEND_TOOL, _SEND_ARGS))
    assert node.sends == 1
    assert node.topic[0].sender == "mcp-caller"

    cli = CliRunner().invoke(
        agent_message_group,
        ["read", "--to", "codex:review", "--thread", "t-9", "--json"],
        obj=runtime,
    )
    assert '"text": "hello"' in cli.output.replace('":"', '": "')

    result = asyncio.run(
        server.call_tool(READ_TOOL, {"to": "codex:review", "thread": "t-9"})
    )
    assert node.reads == 2
    assert result.structured_content is not None
    messages = result.structured_content["messages"]
    assert [m["text"] for m in messages] == ["hello"]


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        (SEND_TOOL, {**_SEND_ARGS, "to": ""}, "missing_recipient"),
        (SEND_TOOL, {**_SEND_ARGS, "text": " "}, "empty_text"),
        (SEND_TOOL, {**_SEND_ARGS, "kind": "gossip"}, "unknown_kind"),
        (SEND_TOOL, {**_SEND_ARGS, "platform": "grok"}, "unknown_platform"),
        (READ_TOOL, {"to": ""}, "missing_recipient"),
    ],
)
def test_refusals_come_back_as_tool_errors(
    tool: str, arguments: dict[str, Any], code: str
) -> None:
    node = FakeAgentMessageNode()
    server = build_server(make_runtime(node))
    with pytest.raises(ToolError, match=f"refused: {code}:"):
        asyncio.run(server.call_tool(tool, arguments))
    assert node.sends == 0
    assert node.reads == 0


def test_read_tool_returns_the_typed_refusals_and_the_legacy_count() -> None:
    from tests.cli.test_agent_message_cli import _node_with_refusals

    node = _node_with_refusals()
    server = build_server(make_runtime(node, {"ONEX_LANE": "mcp-caller"}))
    result = asyncio.run(server.call_tool(READ_TOOL, {"to": "codex:review"}))
    assert result.structured_content is not None
    assert result.structured_content["unverified"] == 2
    assert [r["reason"] for r in result.structured_content["rejections"]] == [
        "sender_mismatch"
    ]
