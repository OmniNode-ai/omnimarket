# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture bindings shared by the Claude Code hook adapter tests (OMN-18531)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_claude_code_hook_adapter_effect.enums import (
    EnumBindingKind,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelClientArgument,
    ModelClientBinding,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_claude_code_hook_adapter_effect/contract.yaml"
)
_CONTRACT = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
COMMAND_TOPIC: str = _CONTRACT["runtime_dispatch"]["command_topic"]
EVENT_TOPIC: str = _CONTRACT["terminal_event"]


def make_binding(**overrides: Any) -> ModelClientBinding:
    fields: dict[str, Any] = {
        "name": "delegation",
        "kind": EnumBindingKind.REPLACING,
        "hook_event": "tool.call",
        "matcher_tool": "Agent",
        "command_topic": COMMAND_TOPIC,
        "command_type": "fixture_command",
        "arguments": (
            ModelClientArgument(name="session_id", source="$.session.id"),
            ModelClientArgument(name="prompt", source="$.input.prompt"),
        ),
        "awaited_events": (EVENT_TOPIC,),
        "verdict_compute": "fixture_verdict",
        "deadline_ms": 30000,
        "probe_rows": ("P0", "P1"),
    }
    fields.update(overrides)
    return ModelClientBinding(**fields)


@pytest.fixture
def replacing_binding() -> ModelClientBinding:
    return make_binding()


@pytest.fixture
def additive_binding() -> ModelClientBinding:
    return make_binding(
        name="session_start",
        kind=EnumBindingKind.ADDITIVE,
        hook_event="SessionStart",
        matcher_tool=None,
        probe_rows=("P16",),
    )
