# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-schema alignment tests — OMN-12112.

Verifies that the command models the orchestrator publishes can be
deserialized into the request models each downstream handler expects,
and that every result model includes ``run_id`` for FSM routing.
"""

from __future__ import annotations

import pytest

# Downstream request models
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_request import (
    ModelSwarmAggregateRequest,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_result import (
    ModelSwarmAggregateResult,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_swarm_decompose_request import (
    ModelSwarmDecomposeRequest,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_swarm_decompose_result import (
    ModelSwarmDecomposeResult,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelEndpointHealth as OrchestratorEndpointHealth,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelSubtask as OrchestratorSubtask,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmAggregateCommand,
    ModelSwarmDecomposeCommand,
    ModelSwarmFanoutCommand,
    ModelSwarmHealthCheckCommand,
    ModelSwarmSelectEndpointsCommand,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmConfig as OrchestratorConfig,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_request import (
    ModelSwarmHealthCheckRequest,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_result import (
    ModelSwarmHealthCheckResult,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_fanout_result import (
    ModelSwarmFanoutResult,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_request import (
    ModelSwarmEndpointSelectionRequest,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_result import (
    ModelSwarmEndpointSelectionResult,
)


@pytest.mark.unit
class TestOrchestratorToHealthCheck:
    """ModelSwarmHealthCheckCommand → ModelSwarmHealthCheckRequest."""

    def test_command_deserializes_into_request(self) -> None:
        cmd = ModelSwarmHealthCheckCommand(
            endpoint_ids=("ep-1", "ep-2"),
            correlation_id="c1",
            run_id="run-1",
        )
        req = ModelSwarmHealthCheckRequest.model_validate(cmd.model_dump())
        assert req.endpoint_ids == ("ep-1", "ep-2")
        assert req.run_id == "run-1"
        assert req.correlation_id == "c1"

    def test_result_has_run_id(self) -> None:
        assert "run_id" in ModelSwarmHealthCheckResult.model_fields


@pytest.mark.unit
class TestOrchestratorToDecompose:
    """ModelSwarmDecomposeCommand → ModelSwarmDecomposeRequest."""

    def test_command_deserializes_into_request(self) -> None:
        cmd = ModelSwarmDecomposeCommand(
            planner_output="plan text",
            planner_model_id="qwen3",
            planner_output_hash="abc123",
            endpoint_ids=("ep-1",),
            original_task="build api",
            correlation_id="c1",
            run_id="run-2",
            decompose=True,
            max_subtasks=5,
        )
        req = ModelSwarmDecomposeRequest.model_validate(cmd.model_dump())
        assert req.run_id == "run-2"
        assert req.planner_output == "plan text"
        assert req.original_task == "build api"

    def test_result_has_run_id(self) -> None:
        assert "run_id" in ModelSwarmDecomposeResult.model_fields


@pytest.mark.unit
class TestOrchestratorToSelectEndpoints:
    """ModelSwarmSelectEndpointsCommand → ModelSwarmEndpointSelectionRequest."""

    def test_command_deserializes_into_request(self) -> None:
        subtask = OrchestratorSubtask(subtask_id="st-1", description="desc")
        health = {
            "ep-1": OrchestratorEndpointHealth(endpoint_id="ep-1", status="reachable"),
        }
        cmd = ModelSwarmSelectEndpointsCommand(
            subtasks=(subtask,),
            endpoint_health=health,
            correlation_id="c1",
            run_id="run-3",
        )
        req = ModelSwarmEndpointSelectionRequest.model_validate(cmd.model_dump())
        assert req.run_id == "run-3"
        assert len(req.subtasks) == 1
        assert req.subtasks[0].subtask_id == "st-1"

    def test_result_has_run_id(self) -> None:
        assert "run_id" in ModelSwarmEndpointSelectionResult.model_fields


@pytest.mark.unit
class TestOrchestratorToFanout:
    """ModelSwarmFanoutCommand → ModelSwarmFanoutRequest."""

    def test_command_deserializes_into_request(self) -> None:
        subtask = OrchestratorSubtask(subtask_id="st-1", description="desc")
        health = {
            "ep-1": OrchestratorEndpointHealth(endpoint_id="ep-1", status="reachable"),
        }
        config = OrchestratorConfig()
        cmd = ModelSwarmFanoutCommand(
            subtasks=(subtask,),
            assignments={"st-1": "ep-1"},
            endpoint_health=health,
            config=config,
            correlation_id="c1",
            run_id="run-4",
        )
        req = ModelSwarmFanoutRequest.model_validate(cmd.model_dump())
        assert req.run_id == "run-4"
        assert req.assignments == {"st-1": "ep-1"}
        assert req.endpoint_health  # forwarded

    def test_result_has_run_id(self) -> None:
        assert "run_id" in ModelSwarmFanoutResult.model_fields


@pytest.mark.unit
class TestOrchestratorToAggregate:
    """ModelSwarmAggregateCommand → ModelSwarmAggregateRequest."""

    def test_command_deserializes_into_request(self) -> None:
        subtask = OrchestratorSubtask(subtask_id="st-1", description="desc")
        cmd = ModelSwarmAggregateCommand(
            subtasks=(subtask,),
            dispatches_json="[]",
            mode="concatenation",
            correlation_id="c1",
            run_id="run-5",
        )
        req = ModelSwarmAggregateRequest.model_validate(cmd.model_dump())
        assert req.run_id == "run-5"
        assert req.dispatches_json == "[]"
        assert req.subtasks[0].subtask_id == "st-1"

    def test_result_has_run_id(self) -> None:
        assert "run_id" in ModelSwarmAggregateResult.model_fields
