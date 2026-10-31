# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_onex_setup_effect declares its topics and is registered as a node and as onex setup."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_onex_setup_effect as setup_node
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    HandlerOnexSetup,
)
from omnimarket.nodes.node_onex_setup_effect.models import ModelOnexSetupRequest

NODE = Path(setup_node.__file__).parent


@pytest.mark.unit
def test_contract_declares_topics_and_entry_points() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["name"] == "node_onex_setup_effect"
    command = contract["event_bus"]["subscribe_topics"][0]
    terminal = contract["terminal_event"]
    assert command == "onex.cmd.omnimarket.onex-setup-requested.v1"
    assert terminal == "onex.evt.omnimarket.onex-setup-completed.v1"
    assert terminal in contract["event_bus"]["publish_topics"]
    assert contract["handler"]["class"] == HandlerOnexSetup.__name__
    pyproject = (NODE.parents[3] / "pyproject.toml").read_text()
    assert (
        'node_onex_setup_effect = "omnimarket.nodes.node_onex_setup_effect"'
        in pyproject
    )
    assert (
        'setup = "omnimarket.nodes.node_onex_setup_effect.cli:setup_command"'
        in pyproject
    )


@pytest.mark.unit
def test_handler_without_a_workspace_fails_the_first_step(tmp_path: Path) -> None:
    result = HandlerOnexSetup().handle(
        ModelOnexSetupRequest(onex_home=tmp_path / ".onex")
    )
    assert result.steps == ()
    assert result.failure is not None
    assert result.failure.step == "workspace"
    assert not (tmp_path / ".onex").exists()
