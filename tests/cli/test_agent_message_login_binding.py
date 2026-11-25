# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A login sends only as the sender registered to its own broker principal (OMN-26644).

The store identity a host holds can read every agent's signing key, so a key alone does not bind a sender to the
login that sends. The registry row names the broker principal each agent name publishes as; the node refuses to sign
for a declared sender whose registered principal is not the login that is sending. Both entry points (the command and
the MCP tool) reach the node through the same runtime, so each is exercised here over the real node handler.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import ToolError

from omnimarket.nodes.node_agent_message_client_effect.adapter_bus_node import (
    agent_message_node_factory,
    bus_runtime,
)
from omnimarket.nodes.node_agent_message_client_effect.cli import agent_message_group
from omnimarket.nodes.node_agent_message_client_effect.mcp_server import (
    READ_TOOL,
    SEND_TOOL,
    build_server,
)
from omnimarket.nodes.node_agent_message_client_effect.runtime import (
    AgentMessageRuntime,
    BusSettings,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from tests.unit.nodes.node_agent_message_effect.agent_directory_fakes import (
    FakeAgentDirectory,
    make_agent,
)
from tests.unit.nodes.node_agent_message_effect.conftest import FakeBus

pytestmark = pytest.mark.unit

LOGIN_A = "dev-host-a"
LOGIN_B = "dev-host-b"
_SEND = [
    "send",
    "--to",
    "lane-r",
    "--thread",
    "t-1",
    "--kind",
    "question",
    "--platform",
    "claude",
    "--text",
    "hello",
    "--bus",
    "inmemory",
]


@pytest.fixture(autouse=True)
def _spool_only_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def directory() -> FakeAgentDirectory:
    """Both agents registered, and this host's store identity holds BOTH keys (the shared-identity case)."""
    a = make_agent("agent-a", f"User:{LOGIN_A}")
    b = make_agent("agent-b", f"User:{LOGIN_B}")
    return FakeAgentDirectory(
        registered={a.name: a.registered(), b.name: b.registered()},
        held_keys={a.name: a.signing_key(), b.name: b.signing_key()},
    )


def _runtime(
    bus: FakeBus,
    tmp_path: Path,
    directory: FakeAgentDirectory,
    environ: dict[str, str],
    login: str | None = LOGIN_A,
) -> AgentMessageRuntime:
    emitter = HandlerEventEmitEffect(publish_adapter=bus, spool_dir=tmp_path / "spool")
    handler = HandlerAgentMessage(
        emitter=emitter,
        bus=bus,
        registry=directory,
        signing_keys=directory,
        login_principal=lambda: login,
        read_window_seconds=0.05,
        quiet_gap_seconds=0.02,
    )
    opened = bus_runtime(
        BusSettings(bus="inmemory"),
        node_factory=lambda: agent_message_node_factory(
            handler_factory=lambda: handler
        ),
    )
    return AgentMessageRuntime(open_node=opened.open_node, environ=environ)


def test_the_honest_sender_is_accepted_on_the_command_line(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    runtime = _runtime(bus, tmp_path, directory, {})
    result = CliRunner().invoke(
        agent_message_group, [*_SEND, "--from", "agent-a"], obj=runtime
    )
    assert result.exit_code == 0, result.output
    assert len(bus.records) == 1
    assert json.loads(bus.records[0][1])["from"] == "agent-a"


def test_a_forged_from_is_refused_on_the_command_line(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    runtime = _runtime(bus, tmp_path, directory, {})
    result = CliRunner().invoke(
        agent_message_group, [*_SEND, "--from", "agent-b"], obj=runtime
    )
    assert result.exit_code == 2, result.output
    assert "sender_mismatch" in result.output
    assert bus.records == []


def test_the_honest_sender_is_accepted_on_the_mcp_tool(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    runtime = _runtime(bus, tmp_path, directory, {"ONEX_LANE": "agent-a"})
    server = build_server(runtime)
    asyncio.run(
        server.call_tool(
            SEND_TOOL,
            {
                "to": "lane-r",
                "thread": "t-1",
                "kind": "question",
                "platform": "claude",
                "text": "hello",
            },
        )
    )
    assert len(bus.records) == 1
    read = asyncio.run(server.call_tool(READ_TOOL, {"to": "lane-r"}))
    assert read.structured_content is not None
    assert [m["from"] for m in read.structured_content["messages"]] == ["agent-a"]


def test_a_forged_sender_is_refused_on_the_mcp_tool(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    runtime = _runtime(bus, tmp_path, directory, {"ONEX_LANE": "agent-b"})
    server = build_server(runtime)
    with pytest.raises(ToolError, match="sender_mismatch"):
        asyncio.run(
            server.call_tool(
                SEND_TOOL,
                {
                    "to": "lane-r",
                    "thread": "t-1",
                    "kind": "question",
                    "platform": "claude",
                    "text": "hello",
                },
            )
        )
    assert bus.records == []


def test_a_login_that_declares_no_principal_is_not_compared(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    """With no SASL user to compare, the existing signature path decides (positive control for the checks above)."""
    runtime = _runtime(bus, tmp_path, directory, {}, login=None)
    result = CliRunner().invoke(
        agent_message_group, [*_SEND, "--from", "agent-b"], obj=runtime
    )
    assert result.exit_code == 0, result.output
