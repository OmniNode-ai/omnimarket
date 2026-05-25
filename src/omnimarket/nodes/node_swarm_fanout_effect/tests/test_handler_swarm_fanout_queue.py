# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmFanout queue dispatch mode."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_swarm_fanout_effect.handlers.handler_swarm_fanout import (
    HandlerSwarmFanout,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.enums import (
    EnumDispatchMode,
    EnumExecutionStatus,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_subtask import ModelSubtask
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_config import (
    ModelSwarmConfig,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)


def _make_publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = MagicMock()
    return pub


def _make_subscriber(results: list[dict[str, object]]) -> MagicMock:
    sub = MagicMock()
    sub.poll = MagicMock(return_value=results)
    return sub


def _make_request(
    subtasks: list[ModelSubtask],
    assignments: dict[str, str],
    endpoints: list[ModelSwarmEndpoint],
    worker_assignments: dict[str, str] | None = None,
    config: ModelSwarmConfig | None = None,
) -> ModelSwarmFanoutRequest:
    return ModelSwarmFanoutRequest(
        subtasks=tuple(subtasks),
        assignments=assignments,
        endpoints=tuple(endpoints),
        config=config or ModelSwarmConfig(),
        correlation_id="corr-queue-test",
        run_id="run-queue-001",
        dispatch_mode=EnumDispatchMode.QUEUE,
        worker_assignments=worker_assignments or {},
    )


@pytest.mark.unit
def test_direct_mode_still_works_regression() -> None:
    """dispatch_mode=direct (default) uses HTTP path unchanged."""
    from unittest.mock import MagicMock

    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    subtask = ModelSubtask(subtask_id="s1", description="do thing")
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": "direct ok"}}]}
    client.post.return_value = resp

    handler = HandlerSwarmFanout(http_client=client)
    request = ModelSwarmFanoutRequest(
        subtasks=(subtask,),
        assignments={"s1": "ep1"},
        endpoints=(ep,),
        config=ModelSwarmConfig(),
        correlation_id="corr-direct",
        run_id="run-direct",
        dispatch_mode=EnumDispatchMode.DIRECT,
    )
    result = handler.handle(request)

    assert len(result.dispatches) == 1
    assert result.dispatches[0].execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.dispatches[0].response_text == "direct ok"
    client.post.assert_called_once()


@pytest.mark.unit
def test_queue_mode_publishes_assignments() -> None:
    """Queue mode publishes one assignment per subtask to the assignment topic."""
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1", base_url="http://localhost:8000/v1", model_id="qwen"
    )
    s1 = ModelSubtask(subtask_id="s1", description="task one")
    s2 = ModelSubtask(subtask_id="s2", description="task two")

    pub = _make_publisher()
    sub = _make_subscriber(
        [
            {
                "run_id": "run-queue-001",
                "subtask_id": "s1",
                "worker_id": "worker-a",
                "execution_status": "succeeded",
                "response_text": "result one",
                "latency_ms": 42,
                "failure_reason": "",
                "model_id": "qwen",
            },
            {
                "run_id": "run-queue-001",
                "subtask_id": "s2",
                "worker_id": "worker-a",
                "execution_status": "succeeded",
                "response_text": "result two",
                "latency_ms": 38,
                "failure_reason": "",
                "model_id": "qwen",
            },
        ]
    )

    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(
        _make_request(
            [s1, s2],
            {"s1": "ep1", "s2": "ep1"},
            [ep],
            worker_assignments={"s1": "worker-a", "s2": "worker-a"},
        )
    )

    assert pub.publish.call_count == 2
    published_topics = {call.args[0] for call in pub.publish.call_args_list}
    assert all("swarm-subtask-assigned" in t for t in published_topics)

    assert len(result.dispatches) == 2
    by_id = {d.subtask_id: d for d in result.dispatches}
    assert by_id["s1"].execution_status == EnumExecutionStatus.SUCCEEDED
    assert by_id["s1"].response_text == "result one"
    assert by_id["s2"].execution_status == EnumExecutionStatus.SUCCEEDED
    assert by_id["s2"].response_text == "result two"


@pytest.mark.unit
def test_queue_mode_timeout_when_no_worker_result() -> None:
    """If worker never returns a result, dispatch is marked TIMEOUT."""
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1", base_url="http://localhost:8000/v1", model_id="m1"
    )
    s1 = ModelSubtask(subtask_id="s1", description="unanswered task")

    pub = _make_publisher()
    sub = _make_subscriber([])  # no results returned

    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1], {"s1": "ep1"}, [ep]))

    assert len(result.dispatches) == 1
    assert result.dispatches[0].execution_status == EnumExecutionStatus.TIMEOUT
    assert result.dispatches[0].failure_reason == "no_result_from_worker"


@pytest.mark.unit
def test_queue_mode_worker_failure_propagates() -> None:
    """A FAILED result from a worker maps to FAILED dispatch."""
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1", base_url="http://localhost:8000/v1", model_id="m1"
    )
    s1 = ModelSubtask(subtask_id="s1", description="failing task")

    pub = _make_publisher()
    sub = _make_subscriber(
        [
            {
                "run_id": "run-queue-001",
                "subtask_id": "s1",
                "worker_id": "worker-a",
                "execution_status": "failed",
                "response_text": "",
                "latency_ms": 10,
                "failure_reason": "connection_refused",
                "model_id": "m1",
            },
        ]
    )

    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1], {"s1": "ep1"}, [ep]))

    assert result.dispatches[0].execution_status == EnumExecutionStatus.FAILED
    assert result.dispatches[0].failure_reason == "connection_refused"


@pytest.mark.unit
def test_queue_mode_requires_publisher_and_subscriber() -> None:
    """queue mode raises ValueError if publisher or subscriber are missing."""
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1", base_url="http://localhost:8000/v1", model_id="m1"
    )
    s1 = ModelSubtask(subtask_id="s1", description="task")
    request = _make_request([s1], {"s1": "ep1"}, [ep])

    with pytest.raises(ValueError, match="queue_publisher"):
        HandlerSwarmFanout().handle(request)


@pytest.mark.unit
def test_queue_mode_dependency_chain_respected() -> None:
    """Dependency-failed subtasks are skipped in queue mode too."""
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1", base_url="http://localhost:8000/v1", model_id="m1"
    )
    s1 = ModelSubtask(subtask_id="s1", description="root")
    s2 = ModelSubtask(subtask_id="s2", description="child", depends_on=("s1",))

    pub = _make_publisher()
    # s1 comes back as failed; s2 should be skipped without being published
    sub = _make_subscriber(
        [
            {
                "run_id": "run-queue-001",
                "subtask_id": "s1",
                "worker_id": "w",
                "execution_status": "failed",
                "response_text": "",
                "latency_ms": 5,
                "failure_reason": "model_error",
                "model_id": "m1",
            },
        ]
    )

    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep]))

    assert len(result.dispatches) == 2
    by_id = {d.subtask_id: d for d in result.dispatches}
    assert by_id["s1"].execution_status == EnumExecutionStatus.FAILED
    assert by_id["s2"].execution_status == EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED
    # s2 must never be published to the assignment topic
    published_subtask_ids = {
        call.args[1]["subtask_id"] for call in pub.publish.call_args_list
    }
    assert "s2" not in published_subtask_ids
