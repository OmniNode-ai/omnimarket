# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered gate evaluator resolves to the typed compute handler."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)

pytestmark = pytest.mark.unit


def test_registered_gate_eval_route() -> None:
    route = _resolve_node_route("node_delegation_gate_eval_compute")
    assert route.command_topic == (
        "onex.cmd.omnimarket.delegation-gate-eval-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.delegation-gate-evaluated.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationGateEval
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelDelegationGateEvalRequest
    )
