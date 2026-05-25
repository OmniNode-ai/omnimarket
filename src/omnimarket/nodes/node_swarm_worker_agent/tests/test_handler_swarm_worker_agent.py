# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmWorkerAgent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.models.swarm.enum_execution_status import EnumExecutionStatus
from omnimarket.models.swarm.model_swarm_subtask_assignment import (
    ModelSwarmSubtaskAssignment,
)
from omnimarket.nodes.node_swarm_worker_agent.handlers.handler_swarm_worker_agent import (
    HandlerSwarmWorkerAgent,
)
from omnimarket.nodes.node_swarm_worker_agent.models.model_worker_agent_request import (
    ModelWorkerAgentRequest,
)


def _make_assignment(
    worker_id: str = "worker-a",
    description: str = "do the thing",
    model_id: str = "qwen",
    endpoint_url: str = "http://localhost:8000/v1",
    timeout_seconds: int = 30,
) -> ModelSwarmSubtaskAssignment:
    return ModelSwarmSubtaskAssignment(
        run_id="run-001",
        subtask_id="s1",
        worker_id=worker_id,
        description=description,
        model_id=model_id,
        endpoint_url=endpoint_url,
        timeout_seconds=timeout_seconds,
        correlation_id="corr-001",
    )


def _make_http_client(text: str = "model response") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    client = MagicMock()
    client.post.return_value = resp
    return client


def _make_publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = MagicMock()
    return pub


@pytest.mark.unit
def test_worker_executes_and_returns_success() -> None:
    client = _make_http_client("hello world")
    pub = _make_publisher()
    assignment = _make_assignment(worker_id="worker-a")
    request = ModelWorkerAgentRequest(worker_id="worker-a", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=pub)
    result = handler.handle(request)

    assert result.subtask_result.execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.subtask_result.response_text == "hello world"
    assert result.subtask_result.subtask_id == "s1"
    assert result.subtask_result.worker_id == "worker-a"
    assert result.subtask_result.latency_ms >= 0


@pytest.mark.unit
def test_worker_publishes_result_to_completion_topic() -> None:
    client = _make_http_client("answer")
    pub = _make_publisher()
    assignment = _make_assignment(worker_id="worker-a")
    request = ModelWorkerAgentRequest(worker_id="worker-a", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=pub)
    result = handler.handle(request)

    pub.publish.assert_called_once()
    topic_arg: str = pub.publish.call_args.args[0]
    assert "swarm-subtask-completed" in topic_arg
    assert "swarm-subtask-completed" in result.published_topic


@pytest.mark.unit
def test_worker_filters_mismatched_worker_id() -> None:
    """Assignment targeted at worker-b must be skipped by worker-a."""
    client = _make_http_client()
    pub = _make_publisher()
    assignment = _make_assignment(worker_id="worker-b")
    request = ModelWorkerAgentRequest(worker_id="worker-a", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=pub)
    result = handler.handle(request)

    assert (
        result.subtask_result.execution_status
        == EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED
    )
    assert result.subtask_result.failure_reason == "worker_id_mismatch"
    client.post.assert_not_called()


@pytest.mark.unit
def test_worker_accepts_unaddressed_assignment() -> None:
    """Assignment with empty worker_id is accepted by any worker."""
    client = _make_http_client("open response")
    pub = _make_publisher()
    assignment = _make_assignment(worker_id="")
    request = ModelWorkerAgentRequest(worker_id="worker-x", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=pub)
    result = handler.handle(request)

    assert result.subtask_result.execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.subtask_result.response_text == "open response"


@pytest.mark.unit
def test_worker_handles_http_failure() -> None:
    client = MagicMock()
    client.post.side_effect = RuntimeError("connection refused")
    pub = _make_publisher()
    assignment = _make_assignment()
    request = ModelWorkerAgentRequest(worker_id="worker-a", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=pub)
    result = handler.handle(request)

    assert result.subtask_result.execution_status == EnumExecutionStatus.FAILED
    assert "connection refused" in result.subtask_result.failure_reason
    # still publishes failure result
    pub.publish.assert_called_once()


@pytest.mark.unit
def test_worker_works_without_publisher() -> None:
    """No publisher — result is returned but not published (fire-and-forget disabled)."""
    client = _make_http_client("no publish")
    assignment = _make_assignment()
    request = ModelWorkerAgentRequest(worker_id="worker-a", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=None)
    result = handler.handle(request)

    assert result.subtask_result.execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.subtask_result.response_text == "no publish"


@pytest.mark.unit
def test_worker_sends_correct_payload_to_endpoint() -> None:
    """Verifies the HTTP POST body matches the assignment description and model."""
    client = _make_http_client("payload check")
    pub = _make_publisher()
    assignment = _make_assignment(
        description="explain quantum entanglement",
        model_id="deepseek-r1",
        endpoint_url="http://localhost:8001/v1",
    )
    request = ModelWorkerAgentRequest(worker_id="worker-a", assignment=assignment)

    handler = HandlerSwarmWorkerAgent(http_client=client, result_publisher=pub)
    handler.handle(request)

    call_kwargs = client.post.call_args
    assert call_kwargs.args[0] == "http://localhost:8001/v1/chat/completions"
    body = call_kwargs.kwargs["json"]
    assert body["model"] == "deepseek-r1"
    assert body["messages"][0]["content"] == "explain quantum entanglement"
