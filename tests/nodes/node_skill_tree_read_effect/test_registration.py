# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered route resolves to the plain typed handler and its request model."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_skill_tree_read_effect.handlers.handler_skill_tree_read import (
    HandlerSkillTreeRead,
)
from omnimarket.nodes.node_skill_tree_read_effect.models import (
    ModelSkillTreeReadRequest,
)

pytestmark = pytest.mark.unit


def test_registered_route() -> None:
    route = _resolve_node_route("node_skill_tree_read_effect")
    assert route.command_topic == "onex.cmd.omnimarket.skill-tree-read-requested.v1"
    assert route.terminal_topic == "onex.evt.omnimarket.skill-tree-read.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerSkillTreeRead
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelSkillTreeReadRequest
    )
