# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment characterization and canonical bus dispatch (OMN-20687)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_runtime_deploy_effect.models.model_runtime_deploy_request import (
    ModelRuntimeDeployOptions,
    ModelRuntimeDeployRequest,
)
from omnimarket.nodes.node_runtime_deploy_effect.node import NodeRuntimeDeployEffect

REPO_ROOT = Path(__file__).parents[3]


def test_node_and_contract_declare_definition_b_route() -> None:
    from omnibase_core.nodes.node_effect import NodeEffect

    assert issubclass(NodeRuntimeDeployEffect, NodeEffect)
    root = REPO_ROOT / "src/omnimarket/nodes/node_runtime_deploy_effect"
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    route = contract["handler_routing"]["handlers"][0]
    assert route["topic"] in contract["event_bus"]["subscribe_topics"]
    assert route["message_category"] == "command"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


def test_options_use_argv_without_shell_interpolation() -> None:
    options = ModelRuntimeDeployOptions(effects_plan="a file; $(touch forbidden)")
    assert options.to_arguments() == ("--effects-plan", "a file; $(touch forbidden)")


def test_repository_root_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ModelRuntimeDeployRequest(
            repository_root=Path("relative"), correlation_id=uuid4()
        )
