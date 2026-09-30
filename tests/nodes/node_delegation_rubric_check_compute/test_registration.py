# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered rubric route resolves to a plain typed handler."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelRubricCheckRequest,
)

pytestmark = pytest.mark.unit


def test_registered_rubric_route():
    route = _resolve_node_route("node_delegation_rubric_check_compute")
    assert (
        route.command_topic
        == "onex.cmd.omnimarket.delegation-rubric-check-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.delegation-rubric-checked.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationRubricCheck
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelRubricCheckRequest
    )
