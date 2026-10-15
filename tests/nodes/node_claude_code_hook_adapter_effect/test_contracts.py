# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three Claude Code adapter node contracts declare their topics and resolve their handlers (OMN-18531, T0.3)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src" / "omnimarket" / "nodes"

# node name -> (command topic, terminal event topic, handler class)
EXPECTED = {
    "node_claude_code_hook_adapter_effect": (
        "onex.cmd.omnimarket.claude-code-hook-adapt.v1",
        "onex.evt.omnimarket.claude-code-hook-adapted.v1",
        "HandlerClaudeCodeHookAdapter",
    ),
    "node_claude_code_binding_render_compute": (
        "onex.cmd.omnimarket.claude-code-binding-render.v1",
        "onex.evt.omnimarket.claude-code-binding-rendered.v1",
        "HandlerClaudeCodeBindingRender",
    ),
    "node_claude_code_binding_install_effect": (
        "onex.cmd.omnimarket.claude-code-binding-install.v1",
        "onex.evt.omnimarket.claude-code-binding-installed.v1",
        "HandlerClaudeCodeBindingInstall",
    ),
}


def _contract(node: str) -> dict:
    return yaml.safe_load((NODES / node / "contract.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("node", sorted(EXPECTED))
def test_the_contract_declares_its_command_and_terminal_event(node: str) -> None:
    command, event, _ = EXPECTED[node]
    contract = _contract(node)
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [event]
    assert contract["runtime_dispatch"]["command_topic"] == command
    assert contract["terminal_event"] == event


@pytest.mark.parametrize("node", sorted(EXPECTED))
def test_the_contract_handler_resolves_to_a_class_with_handle(node: str) -> None:
    _, _, class_name = EXPECTED[node]
    handler = _contract(node)["handler"]
    assert handler["class"] == class_name
    cls = getattr(importlib.import_module(handler["module"]), class_name)
    assert callable(cls.handle)


@pytest.mark.parametrize("node", sorted(EXPECTED))
def test_the_node_is_registered_as_an_onex_node_entry_point(node: str) -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[node] == f"omnimarket.nodes.{node}"


def test_the_hook_console_script_points_at_the_adapter_entry() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    target = pyproject["project"]["scripts"]["onex-claude-code-hook"]
    module, _, attr = target.partition(":")
    assert _contract("node_claude_code_hook_adapter_effect")["entry_point"] == {
        "console_script": "onex-claude-code-hook",
        "module": module,
        "function": attr,
        "stdin": "one hook JSON document",
        "stdout": "only the hook's output shape ({result}, {deny}, {messages}, hookSpecificOutput.additionalContext, or nothing)",
    }
