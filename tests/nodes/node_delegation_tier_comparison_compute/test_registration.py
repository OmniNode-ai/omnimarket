# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered tier comparison node resolves to the typed compute handler."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.delegation_tier_comparison.model_comparison_request import (
    ModelComparisonRequest,
)
from omnimarket.nodes.node_delegation_tier_comparison_compute.handlers.handler_delegation_tier_comparison import (
    HandlerDelegationTierComparison,
)

pytestmark = pytest.mark.unit


def test_registered_tier_comparison_route() -> None:
    route = _resolve_node_route("node_delegation_tier_comparison_compute")
    assert route.command_topic == (
        "onex.cmd.omnimarket.delegation-tier-comparison-requested.v1"
    )
    assert route.terminal_topic == (
        "onex.evt.omnimarket.delegation-tier-comparison-measured.v1"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationTierComparison
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelComparisonRequest
    )
