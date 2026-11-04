# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered route resolves to the plain typed handler, its models and the declared topics."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.network_graph import (
    ModelNetworkGraphDocument,
    ModelNetworkGraphRequest,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_network_graph_compute import (
    HandlerNetworkGraphCompute,
)

from .support import REFERENCE_TIME, bus_text, contract, request

pytestmark = pytest.mark.unit


def test_registered_route() -> None:
    route = _resolve_node_route("node_network_graph_compute")
    assert (
        route.command_topic == "onex.cmd.omnimarket.network-graph-compute-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.network-graph-computed.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerNetworkGraphCompute
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelNetworkGraphRequest
    )


def test_document_is_generated_at_the_reference_time() -> None:
    document = HandlerNetworkGraphCompute().handle(
        request(contract("r", "a/contract.yaml", bus_text("a", ("t.x",), ())))
    )
    assert isinstance(document, ModelNetworkGraphDocument)
    assert document.generated_at == REFERENCE_TIME
