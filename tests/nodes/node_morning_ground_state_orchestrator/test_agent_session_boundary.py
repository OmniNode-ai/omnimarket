# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Morning lab units reach the delegation effect's agent transport."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerAgentSession,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_agent_session as session,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_agent_session import (
    main,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_phase_lab_work import (
    AGENT_LAUNCHER,
)


@pytest.mark.unit
def test_lab_launcher_is_the_standalone_delegation_effect() -> None:
    assert AGENT_LAUNCHER == (
        "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers/"
        "handler_agent_session.py"
    )
    source = (Path(__file__).resolve().parents[3] / AGENT_LAUNCHER).read_text()
    assert 'if __name__ == "__main__":' in source


@pytest.mark.unit
def test_delegation_effect_declares_its_agent_operation() -> None:
    node = Path(__file__).resolve().parents[3] / (
        "src/omnimarket/nodes/node_llm_delegation_call_effect"
    )
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["agent_invocation"] is True
    assert "HandlerAgentSession" in contract["description"]


@pytest.mark.unit
def test_agent_transport_keeps_permissions_and_preserves_literal_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []

    def run(
        argv: list[str], *, input: str, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert text is True
        assert check is False
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(subprocess, "run", run)
    brief = "a" * 20_000 + "\n$(touch forbidden); 'literal'"
    assert main(["sonnet", brief[:20_000], brief[20_000:]]) == 7
    ((argv, stdin),) = calls
    assert argv == [
        "claude",
        "-p",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "text",
        "--model",
        "sonnet",
    ]
    assert stdin == brief


@pytest.mark.unit
@pytest.mark.parametrize("argv", [[], ["sonnet"]])
def test_incomplete_launcher_input_never_starts_a_session(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(self: HandlerAgentSession, model: str, brief: str) -> int:
        pytest.fail("incomplete input started a session")

    monkeypatch.setattr(HandlerAgentSession, "run", forbidden)
    assert main(argv) == 64


# --- tools-only sessions (RULING 2026-10-21T22:32:15Z, OMN-17427) ------------------------

BUILTIN_TOOLS = frozenset(
    {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "Task",
        "Skill",
    }
)


def _config(tmp_path: Path, servers: dict[str, object]) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


@pytest.mark.unit
def test_a_tools_only_session_has_no_built_in_tool_and_one_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["input"] == "the brief"
        return subprocess.CompletedProcess(argv, 0, '{"structured_output": {}}', "")

    monkeypatch.setattr(subprocess, "run", run)
    schema = tmp_path / "schema.json"
    schema.write_text('{"type": "object"}')
    config = _config(tmp_path, {"morning": {"command": "python3", "args": []}})
    done = session.HandlerAgentSession().run_tools_only(
        "sonnet", "the brief", str(config), str(schema)
    )
    assert done.returncode == 0
    ((argv,),) = [calls]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__morning"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert not BUILTIN_TOOLS & set(argv)
    assert not any("skip-permissions" in a or "bypass" in a.lower() for a in argv)


@pytest.mark.unit
@pytest.mark.parametrize(
    "servers",
    [{}, {"a": {}, "b": {}}, {"bad name": {}}],
    ids=["none", "two", "not-a-name"],
)
def test_a_tools_only_session_needs_exactly_one_named_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict[str, object]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("session started")

    monkeypatch.setattr(subprocess, "run", forbidden)
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    with pytest.raises(session.AgentSessionRefusedError):
        session.HandlerAgentSession().run_tools_only(
            "sonnet", "x", str(_config(tmp_path, servers)), str(schema)
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--tools", "Bash"),
        ("--tools", "default"),
        ("--allowedTools", "Bash(git *)"),
        ("--allowedTools", "mcp__morning Bash"),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode", "acceptEdits"),
        ("--setting-sources", "user,project"),
    ],
)
def test_the_tools_only_guard_refuses_a_shell_or_a_wider_grant(
    flag: str, value: str
) -> None:
    argv = session.tools_only_argv("sonnet", "mcp.json", "morning", "{}")
    session.refuse_unless_tools_only(argv, "morning")
    argv[argv.index(flag) + 1] = value
    with pytest.raises(
        session.AgentSessionRefusedError, match="refused a tools-only session"
    ):
        session.refuse_unless_tools_only(argv, "morning")


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        ["--dangerously-skip-permissions"],
        ["--add-dir", "/"],
        ["--allowedTools", "Bash"],
        ["--settings", "{}"],
    ],
)
def test_the_tools_only_guard_refuses_added_power(extra: list[str]) -> None:
    argv = session.tools_only_argv("sonnet", "mcp.json", "morning", "{}") + extra
    with pytest.raises(session.AgentSessionRefusedError):
        session.refuse_unless_tools_only(argv, "morning")
    stripped = session.tools_only_argv("sonnet", "mcp.json", "morning", "{}")
    stripped.remove("--strict-mcp-config")
    with pytest.raises(session.AgentSessionRefusedError, match="strict-mcp-config"):
        session.refuse_unless_tools_only(stripped, "morning")


@pytest.mark.unit
def test_the_standalone_entrypoint_runs_a_tools_only_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("sys.stdin", io.StringIO("brief"))
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    config = _config(tmp_path, {"morning": {}})
    assert main(["--tools-only", "sonnet", str(config), str(schema)]) == 0
    assert capsys.readouterr().out == '{"ok": true}'
    assert main(["--tools-only", "sonnet"]) == 64
