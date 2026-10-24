# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered route resolves to the plain typed handler and its request model."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_standup_draft_check_compute.handlers.handler_standup_draft_check import (
    HandlerStandupDraftCheck,
)
from omnimarket.nodes.node_standup_draft_check_compute.models import (
    ModelStandupDraftCheckRequest,
)

pytestmark = pytest.mark.unit


def test_registered_route() -> None:
    route = _resolve_node_route("node_standup_draft_check_compute")
    assert route.command_topic == "onex.cmd.omnimarket.standup-draft-check-requested.v1"
    assert route.terminal_topic == "onex.evt.omnimarket.standup-draft-checked.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerStandupDraftCheck
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelStandupDraftCheckRequest
    )
