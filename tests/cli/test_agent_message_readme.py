# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every command, tool name, enum value and exit code the client README gives is the real one."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.enums.enum_agent_message_kind import EnumAgentMessageKind
from omnimarket.enums.enum_agent_message_platform import EnumAgentMessagePlatform
from omnimarket.nodes.node_agent_message_client_effect.cli import agent_message_group
from omnimarket.nodes.node_agent_message_client_effect.mcp_server import (
    READ_TOOL,
    SEND_TOOL,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    REFUSAL_EXIT_CODES,
)
from tests.cli.agent_message_fakes import FakeAgentMessageNode, make_runtime

pytestmark = pytest.mark.unit

README = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_agent_message_client_effect/README.txt"
)


def _blocks(language: str, text: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _commands(text: str) -> list[list[str]]:
    """Each ``onex agent-message ...`` command in the bash blocks, split like a shell."""
    found: list[list[str]] = []
    for block in _blocks("bash", text):
        joined = block.replace("\\\n", " ")
        for line in joined.splitlines():
            words = shlex.split(line)
            if words[:2] == ["onex", "agent-message"]:
                found.append(words[2:])
    return found


def test_readme_command_line_examples_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "answer.txt").write_text("the diff is fine\n")
    commands = [c for c in _commands(README.read_text()) if c[0] != "serve-mcp"]
    assert [c[0] for c in commands] == ["send", "send", "read"]
    node = FakeAgentMessageNode()
    for args in commands:
        result = CliRunner().invoke(
            agent_message_group,
            args,
            obj=make_runtime(node, {"ONEX_LANE": "readme-lane"}),
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"{args}: {result.output}"
    assert node.sends == 2
    assert node.reads == 1


def test_readme_registrations_name_the_real_server_command() -> None:
    text = README.read_text()
    assert "onex agent-message serve-mcp --bus-lane dev" in text
    assert "serve-mcp" in agent_message_group.commands
    codex = tomllib.loads("\n".join(_blocks("toml", text)[:1]))
    server = codex["mcp_servers"]["agent-message"]
    assert [server["command"], *server["args"]] == [
        "onex",
        "agent-message",
        "serve-mcp",
        "--bus-lane",
        "dev",
    ]
    approvals = tomllib.loads(_blocks("toml", text)[1])["mcp_servers"]["agent-message"][
        "tools"
    ]
    assert set(approvals) == {SEND_TOOL, READ_TOOL}
    gemini = json.loads(_blocks("json", text)[0])["mcpServers"]["agent-message"]
    assert [gemini["command"], *gemini["args"]] == [
        "onex",
        "agent-message",
        "serve-mcp",
        "--bus-lane",
        "dev",
    ]
    assert f"mcp__agent-message__{SEND_TOOL}" in text
    assert f"mcp__agent-message__{READ_TOOL}" in text


def test_readme_enum_values_and_exit_codes_match_the_code() -> None:
    text = README.read_text()
    for kind in EnumAgentMessageKind:
        assert f"`{kind.value}`" in text
    for platform in EnumAgentMessagePlatform:
        assert f"`{platform.value}`" in text
    rows = re.findall(r"^\| (`[^|]+`) \| (\d) \|$", text, flags=re.MULTILINE)
    documented = {
        code: int(exit_code)
        for cell, exit_code in rows
        for code in re.findall(r"`(\w+)`", cell)
    }
    assert documented == {c.value: n for c, n in REFUSAL_EXIT_CODES.items()}
