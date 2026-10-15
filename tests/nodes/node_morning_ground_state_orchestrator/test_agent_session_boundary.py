# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Morning lab units reach the delegation effect's agent transport."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerAgentSession,
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
