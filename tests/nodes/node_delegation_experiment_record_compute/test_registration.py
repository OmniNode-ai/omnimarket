# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered experiment record node resolves to the typed compute handler."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.delegation_experiment_record.model_experiment_record_request import (
    ModelExperimentRecordRequest,
)
from omnimarket.nodes.node_delegation_experiment_record_compute.handlers.handler_delegation_experiment_record import (
    HandlerDelegationExperimentRecord,
)

pytestmark = pytest.mark.unit


def test_registered_experiment_record_route() -> None:
    route = _resolve_node_route("node_delegation_experiment_record_compute")
    assert route.command_topic == (
        "onex.cmd.omnimarket.delegation-experiment-record-requested.v1"
    )
    assert route.terminal_topic == (
        "onex.evt.omnimarket.delegation-experiment-record-checked.v1"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationExperimentRecord
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelExperimentRecordRequest
    )
