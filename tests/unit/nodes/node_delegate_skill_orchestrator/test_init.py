# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public export tests for node_delegate_skill_orchestrator."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


@pytest.mark.unit
def test_public_exports() -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator import (
        HandlerDelegateSkill,
        ModelDelegateSkillRequest,
        ModelDelegateSkillResponse,
        ModelDelegateSkillResponseMetrics,
        ProtocolDelegationDispatchPort,
    )

    assert HandlerDelegateSkill is not None
    assert ModelDelegateSkillRequest is not None
    assert ModelDelegateSkillResponse is not None
    assert ModelDelegateSkillResponseMetrics is not None
    assert ProtocolDelegationDispatchPort is not None


@pytest.mark.unit
def test_pyproject_registers_node_and_cli_entry_points() -> None:
    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    nodes = data["project"]["entry-points"]["onex.nodes"]
    assert (
        nodes["node_delegate_skill_orchestrator"]
        == "omnimarket.nodes.node_delegate_skill_orchestrator"
    )
    scripts = data["project"]["scripts"]
    assert scripts["onex-delegate"] == "omnimarket.adapters.claude_code.delegate:main"
