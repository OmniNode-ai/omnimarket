# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model validation tests for node_swarm_fanout_orchestrator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_swarm_fanout_orchestrator.models.enums import (
    EnumExecutionStatus,
    EnumFanoutFsmState,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_config import (
    ModelSwarmConfig,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_result import (
    ModelSwarmFanoutResult,
)


@pytest.mark.unit
def test_model_subtask_defaults() -> None:
    s = ModelSubtask(subtask_id="x", description="desc")
    assert s.depends_on == ()
    assert s.estimated_tokens == 0
    assert s.category == "general"


@pytest.mark.unit
def test_model_subtask_frozen() -> None:
    s = ModelSubtask(subtask_id="x", description="desc")
    with pytest.raises((ValidationError, TypeError)):
        s.subtask_id = "y"  # type: ignore[misc]


@pytest.mark.unit
def test_model_swarm_config_defaults() -> None:
    cfg = ModelSwarmConfig()
    assert cfg.max_parallel_subtasks == 4
    assert cfg.fallback_policy_enabled is True


@pytest.mark.unit
def test_model_swarm_dispatch_required_fields() -> None:
    d = ModelSwarmDispatch(
        subtask_id="s1",
        endpoint_id="ep1",
        model_id="m1",
        base_url="http://x",
        execution_status=EnumExecutionStatus.SUCCEEDED,
    )
    assert d.response_text == ""
    assert d.retry_count == 0
    assert d.wave == 0


@pytest.mark.unit
def test_model_swarm_dispatch_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelSwarmDispatch(  # type: ignore[call-arg]
            subtask_id="s1",
            endpoint_id="ep1",
            model_id="m1",
            base_url="http://x",
            execution_status=EnumExecutionStatus.SUCCEEDED,
            unknown_field="bad",
        )


@pytest.mark.unit
def test_model_swarm_endpoint_endpoint_ref_default_empty() -> None:
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://x", model_id="m1")
    assert ep.endpoint_ref == ""


@pytest.mark.unit
def test_model_swarm_endpoint_with_ref() -> None:
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1",
        base_url="http://x",
        model_id="m1",
        endpoint_ref="LLM_LOCAL_PRIMARY_URL",
    )
    assert ep.endpoint_ref == "LLM_LOCAL_PRIMARY_URL"


@pytest.mark.unit
def test_model_swarm_fanout_request_roundtrip() -> None:
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1",
        base_url="http://ep1",
        model_id="m1",
        endpoint_ref="LLM_LOCAL_PRIMARY_URL",
    )
    s = ModelSubtask(subtask_id="s1", description="task")
    req = ModelSwarmFanoutRequest(
        subtasks=(s,),
        assignments={"s1": "ep1"},
        endpoints=(ep,),
        config=ModelSwarmConfig(),
        correlation_id="c1",
        run_id="r1",
    )
    assert req.subtasks[0].subtask_id == "s1"
    assert req.assignments["s1"] == "ep1"


@pytest.mark.unit
def test_model_swarm_fanout_request_no_dispatch_mode() -> None:
    """ModelSwarmFanoutRequest must reject dispatch_mode (removed field)."""
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://x", model_id="m1")
    s = ModelSubtask(subtask_id="s1", description="task")
    with pytest.raises(ValidationError):
        ModelSwarmFanoutRequest(  # type: ignore[call-arg]
            subtasks=(s,),
            assignments={"s1": "ep1"},
            endpoints=(ep,),
            config=ModelSwarmConfig(),
            correlation_id="c1",
            run_id="r1",
            dispatch_mode="direct",
        )


@pytest.mark.unit
def test_model_swarm_fanout_request_no_worker_assignments() -> None:
    """ModelSwarmFanoutRequest must reject worker_assignments (removed field)."""
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://x", model_id="m1")
    s = ModelSubtask(subtask_id="s1", description="task")
    with pytest.raises(ValidationError):
        ModelSwarmFanoutRequest(  # type: ignore[call-arg]
            subtasks=(s,),
            assignments={"s1": "ep1"},
            endpoints=(ep,),
            config=ModelSwarmConfig(),
            correlation_id="c1",
            run_id="r1",
            worker_assignments={"s1": "w1"},
        )


@pytest.mark.unit
def test_model_swarm_fanout_result_terminal_fields() -> None:
    d1 = ModelSwarmDispatch(
        subtask_id="s1",
        endpoint_id="ep1",
        model_id="m1",
        base_url="http://x",
        execution_status=EnumExecutionStatus.SUCCEEDED,
        latency_ms=100,
    )
    d2 = ModelSwarmDispatch(
        subtask_id="s2",
        endpoint_id="ep1",
        model_id="m1",
        base_url="http://x",
        execution_status=EnumExecutionStatus.FAILED,
        latency_ms=50,
    )
    result = ModelSwarmFanoutResult(
        dispatches=(d1, d2),
        wall_latency_ms=120,
        sum_subtask_latency_ms=150,
        completed_count=1,
        failed_count=1,
        degraded=True,
        aggregation_mode="collect_all",
    )
    assert result.sum_subtask_latency_ms == 150
    assert len(result.dispatches) == 2
    assert result.completed_count == 1
    assert result.failed_count == 1
    assert result.degraded is True
    assert result.aggregation_mode == "collect_all"


@pytest.mark.unit
def test_enum_execution_status_values() -> None:
    assert EnumExecutionStatus.SUCCEEDED.value == "succeeded"
    assert EnumExecutionStatus.FAILED.value == "failed"
    assert EnumExecutionStatus.TIMEOUT.value == "timeout"
    assert (
        EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED.value
        == "skipped_dependency_failed"
    )


@pytest.mark.unit
def test_enum_fanout_fsm_state_values() -> None:
    assert EnumFanoutFsmState.PLANNING.value == "PLANNING"
    assert EnumFanoutFsmState.DISPATCHING.value == "DISPATCHING"
    assert EnumFanoutFsmState.COLLECTING.value == "COLLECTING"
    assert EnumFanoutFsmState.COMPLETED.value == "COMPLETED"
    assert EnumFanoutFsmState.FAILED.value == "FAILED"
