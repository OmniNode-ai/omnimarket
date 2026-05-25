# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Additional orchestrator tests — multi-subtask collection, endpoint_ref passthrough."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_swarm_fanout_orchestrator.handlers.handler_swarm_fanout import (
    HandlerSwarmFanout,
    _correlation_id,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.enums import (
    EnumExecutionStatus,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_config import (
    ModelSwarmConfig,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)


def _make_publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = MagicMock()
    return pub


def _make_subscriber(events: list[dict[str, object]]) -> MagicMock:
    sub = MagicMock()
    sub.poll = MagicMock(return_value=events)
    return sub


def _make_endpoint(
    ep_id: str = "ep1",
    ref: str = "LLM_LOCAL_PRIMARY_URL",
    model_id: str = "qwen",
) -> ModelSwarmEndpoint:
    return ModelSwarmEndpoint(
        endpoint_id=ep_id,
        base_url="http://localhost:8000/v1",
        model_id=model_id,
        endpoint_ref=ref,
    )


def _make_request(
    subtasks: list[ModelSubtask],
    assignments: dict[str, str],
    endpoints: list[ModelSwarmEndpoint],
    config: ModelSwarmConfig | None = None,
) -> ModelSwarmFanoutRequest:
    return ModelSwarmFanoutRequest(
        subtasks=tuple(subtasks),
        assignments=assignments,
        endpoints=tuple(endpoints),
        config=config or ModelSwarmConfig(),
        correlation_id="corr-queue-test",
        run_id="run-queue-001",
    )


def _ok_event(subtask_id: str, model_id: str = "qwen") -> dict[str, object]:
    return {
        "correlation_id": _correlation_id("run-queue-001", subtask_id),
        "success": True,
        "model_id": model_id,
        "latency_ms": 40,
        "_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
    }


@pytest.mark.unit
def test_two_subtasks_both_succeed() -> None:
    ep = _make_endpoint()
    s1 = ModelSubtask(subtask_id="s1", description="task one")
    s2 = ModelSubtask(subtask_id="s2", description="task two")

    pub = _make_publisher()
    sub = _make_subscriber([_ok_event("s1"), _ok_event("s2")])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep]))

    assert pub.publish.call_count == 2
    published_topics = {call.args[0] for call in pub.publish.call_args_list}
    assert all("delegation-execute" in t for t in published_topics)

    assert len(result.dispatches) == 2
    by_id = {d.subtask_id: d for d in result.dispatches}
    assert by_id["s1"].execution_status == EnumExecutionStatus.SUCCEEDED
    assert by_id["s2"].execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.completed_count == 2
    assert result.failed_count == 0


@pytest.mark.unit
def test_endpoint_ref_passed_through_in_command() -> None:
    """endpoint_ref from registry is forwarded in the delegation-execute payload."""
    ep = _make_endpoint(ref="LLM_CODER_FAST_URL")
    s = ModelSubtask(subtask_id="s1", description="task")
    pub = _make_publisher()
    sub = _make_subscriber([_ok_event("s1")])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    _, payload = pub.publish.call_args.args
    assert payload["endpoint_ref"] == "LLM_CODER_FAST_URL"


@pytest.mark.unit
def test_timeout_when_no_terminal_event() -> None:
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="unanswered task")
    pub = _make_publisher()
    sub = _make_subscriber([])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    assert len(result.dispatches) == 1
    assert result.dispatches[0].execution_status == EnumExecutionStatus.TIMEOUT
    assert "no_terminal_event" in result.dispatches[0].failure_reason


@pytest.mark.unit
def test_all_tiers_failed_event_marks_dispatch_failed() -> None:
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="failing task")
    pub = _make_publisher()
    sub = _make_subscriber(
        [
            {
                "correlation_id": _correlation_id("run-queue-001", "s1"),
                "success": False,
                "model_id": "",
                "latency_ms": 10,
                "failure_class": "connection_refused",
                "_topic": "onex.evt.omnimarket.delegation-all-tiers-failed.v1",
            }
        ]
    )
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    assert result.dispatches[0].execution_status == EnumExecutionStatus.FAILED


@pytest.mark.unit
def test_requires_publisher_and_subscriber() -> None:
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="task")
    req = _make_request([s], {"s1": "ep1"}, [ep])

    with pytest.raises(ValueError, match="queue_publisher"):
        HandlerSwarmFanout().handle(req)


@pytest.mark.unit
def test_dependency_chain_s2_not_published_when_s1_fails() -> None:
    ep = _make_endpoint()
    s1 = ModelSubtask(subtask_id="s1", description="root")
    s2 = ModelSubtask(subtask_id="s2", description="child", depends_on=("s1",))

    pub = _make_publisher()
    sub = _make_subscriber(
        [
            {
                "correlation_id": _correlation_id("run-queue-001", "s1"),
                "success": False,
                "model_id": "",
                "latency_ms": 5,
                "failure_class": "model_error",
                "_topic": "onex.evt.omnimarket.delegation-all-tiers-failed.v1",
            }
        ]
    )
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep]))

    assert len(result.dispatches) == 2
    by_id = {d.subtask_id: d for d in result.dispatches}
    assert by_id["s1"].execution_status == EnumExecutionStatus.FAILED
    assert by_id["s2"].execution_status == EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED

    published_task_ids = {
        str(call.args[1].get("task_id", "")) for call in pub.publish.call_args_list
    }
    assert "s2" not in published_task_ids


@pytest.mark.unit
def test_dispatch_mode_field_rejected() -> None:
    """ModelSwarmFanoutRequest has extra='forbid' — dispatch_mode must fail validation."""
    from pydantic import ValidationError

    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="task")
    with pytest.raises(ValidationError):
        ModelSwarmFanoutRequest(
            subtasks=(s,),
            assignments={"s1": "ep1"},
            endpoints=(ep,),
            config=ModelSwarmConfig(),
            correlation_id="c",
            run_id="r",
            dispatch_mode="direct",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_worker_assignments_field_rejected() -> None:
    """worker_assignments is removed — extra='forbid' must raise."""
    from pydantic import ValidationError

    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="task")
    with pytest.raises(ValidationError):
        ModelSwarmFanoutRequest(
            subtasks=(s,),
            assignments={"s1": "ep1"},
            endpoints=(ep,),
            config=ModelSwarmConfig(),
            correlation_id="c",
            run_id="r",
            worker_assignments={"s1": "w1"},  # type: ignore[call-arg]
        )
