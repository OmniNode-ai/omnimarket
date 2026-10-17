# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered brief drafting node resolves to the typed assemble handler."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.lane_brief_draft.model_lane_brief_assemble_request import (
    ModelLaneBriefAssembleRequest,
)
from omnimarket.nodes.node_lane_brief_draft_compute.handlers.handler_lane_brief_assemble import (
    HandlerLaneBriefAssemble,
)

pytestmark = pytest.mark.unit


def test_registered_lane_brief_draft_route() -> None:
    route = _resolve_node_route("node_lane_brief_draft_compute")
    assert route.command_topic == "onex.cmd.omnimarket.lane-brief-draft-requested.v1"
    assert route.terminal_topic == "onex.evt.omnimarket.lane-brief-draft-completed.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerLaneBriefAssemble
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelLaneBriefAssembleRequest
    )
