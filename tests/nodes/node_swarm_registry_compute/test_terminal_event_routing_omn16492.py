# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A real endpoint selection is routed to the contract's terminal topic.

OMN-16492: state coverage for ``node_swarm_registry_compute``'s single output
state. The compute handler is pure — the runtime publishes its
``ModelSwarmEndpointSelectionResult`` to the contract-declared terminal event,
so the load-bearing binding is the contract routing itself, asserted here
alongside an actual selection so the output state is exercised, not merely
named.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_swarm_registry_compute.handlers.handler_swarm_registry import (
    HandlerSwarmRegistry,
)
from omnimarket.nodes.node_swarm_registry_compute.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_request import (
    ModelEndpointHealth,
    ModelSubtask,
    ModelSwarmEndpointSelectionRequest,
)

_NODE_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_swarm_registry_compute"
    / "contract.yaml"
)

_REGISTRY_YAML = textwrap.dedent(
    """\
    registry_schema_version: "1.0.0"
    endpoints:
      - id: "code-ep"
        base_url: "http://localhost:8000/v1"
        model_id: "some-coder"
        provider: "sglang"
        capabilities: [code_generation, refactoring, analysis]
        context_window: 100000
        cost_basis: "local"
    """
)


@pytest.mark.unit
def test_selection_result_routes_to_selected_terminal_topic(tmp_path: Path) -> None:
    registry_path = tmp_path / "endpoint_registry.yaml"
    registry_path.write_text(_REGISTRY_YAML)
    handler = HandlerSwarmRegistry(registry_path=registry_path)

    request = ModelSwarmEndpointSelectionRequest(
        subtasks=(
            ModelSubtask(
                subtask_id="t1",
                description="Task t1",
                category="code_generation",
                estimated_tokens=0,
                model_affinity="",
            ),
        ),
        endpoint_health={
            "code-ep": ModelEndpointHealth(
                endpoint_id="code-ep",
                endpoint_status=EnumEndpointStatus.reachable,
                model_status=EnumModelStatus.available,
            )
        },
        registry_hash="abc123",
    )
    result = handler.handle(request)
    assert result.assignments["t1"] == "code-ep"

    contract = yaml.safe_load(_NODE_CONTRACT_PATH.read_text(encoding="utf-8"))
    selected_topic = "onex.evt.omnimarket.swarm-endpoints-selected.v1"
    assert contract["terminal_event"] == selected_topic
    assert selected_topic in contract["event_bus"]["publish_topics"]
    assert selected_topic in contract["externally_consumed_topics"]
