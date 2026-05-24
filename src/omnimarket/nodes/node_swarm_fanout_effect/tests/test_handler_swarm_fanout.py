# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmFanout — wave scheduling, bounded concurrency, retry, fallback."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_swarm_fanout_effect.handlers.handler_swarm_fanout import (
    HandlerSwarmFanout,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.enums import EnumExecutionStatus
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


def _make_response(text: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _make_client(text: str = "ok") -> MagicMock:
    client = MagicMock()
    client.post.return_value = _make_response(text)
    return client


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
        correlation_id="corr-123",
        run_id="run-abc",
    )


@pytest.mark.unit
def test_single_subtask_success() -> None:
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    subtask = ModelSubtask(subtask_id="s1", description="do thing")
    client = _make_client("result text")
    handler = HandlerSwarmFanout(http_client=client)

    result = handler.handle(_make_request([subtask], {"s1": "ep1"}, [ep]))

    assert len(result.dispatches) == 1
    d = result.dispatches[0]
    assert d.subtask_id == "s1"
    assert d.execution_status == EnumExecutionStatus.SUCCEEDED
    assert d.response_text == "result text"
    assert d.endpoint_id == "ep1"
    assert d.latency_ms >= 0
    assert result.wall_latency_ms >= 0


@pytest.mark.unit
def test_wave_ordering_dependency_runs_after() -> None:
    """Subtask with depends_on must run in a later wave."""
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    wave_order: list[str] = []

    def side_effect(url: str, *, json: Any, timeout: float) -> MagicMock:
        task_content = json["messages"][0]["content"]
        wave_order.append(task_content)
        return _make_response("ok")

    client = MagicMock()
    client.post.side_effect = side_effect

    s1 = ModelSubtask(subtask_id="s1", description="first", depends_on=())
    s2 = ModelSubtask(subtask_id="s2", description="second", depends_on=("s1",))

    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(_make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep]))

    assert len(result.dispatches) == 2
    assert all(
        d.execution_status == EnumExecutionStatus.SUCCEEDED for d in result.dispatches
    )
    # "first" must appear before "second" in dispatch order
    assert wave_order.index("first") < wave_order.index("second")
    # Waves must differ
    wave_by_id = {d.subtask_id: d.wave for d in result.dispatches}
    assert wave_by_id["s1"] < wave_by_id["s2"]


@pytest.mark.unit
def test_dependency_failed_skips_dependent() -> None:
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    client = MagicMock()
    client.post.side_effect = RuntimeError("endpoint down")

    s1 = ModelSubtask(subtask_id="s1", description="first", depends_on=())
    s2 = ModelSubtask(subtask_id="s2", description="second", depends_on=("s1",))

    config = ModelSwarmConfig(retry_policy_max_retries=0, fallback_policy_enabled=False)
    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(
        _make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep], config)
    )

    assert len(result.dispatches) == 2
    by_id = {d.subtask_id: d for d in result.dispatches}
    assert by_id["s1"].execution_status == EnumExecutionStatus.FAILED
    assert by_id["s2"].execution_status == EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED


@pytest.mark.unit
def test_bounded_concurrency_max_parallel() -> None:
    """max_parallel_subtasks=1 means subtasks run serially."""
    import threading

    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    concurrent_count = 0
    max_concurrent = 0
    lock = threading.Lock()

    def side_effect(url: str, *, json: Any, timeout: float) -> MagicMock:
        nonlocal concurrent_count, max_concurrent
        with lock:
            concurrent_count += 1
            if concurrent_count > max_concurrent:
                max_concurrent = concurrent_count
        import time

        time.sleep(0.01)
        with lock:
            concurrent_count -= 1
        return _make_response("ok")

    client = MagicMock()
    client.post.side_effect = side_effect

    subtasks = [
        ModelSubtask(subtask_id=f"s{i}", description=f"task{i}") for i in range(4)
    ]
    assignments = {s.subtask_id: "ep1" for s in subtasks}
    config = ModelSwarmConfig(max_parallel_subtasks=1, max_subtasks_per_endpoint=4)
    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(_make_request(subtasks, assignments, [ep], config))

    assert all(
        d.execution_status == EnumExecutionStatus.SUCCEEDED for d in result.dispatches
    )
    assert max_concurrent == 1


@pytest.mark.unit
def test_retry_on_failure_succeeds_second_attempt() -> None:
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    call_count = 0

    def side_effect(url: str, *, json: Any, timeout: float) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient error")
        return _make_response("retry ok")

    client = MagicMock()
    client.post.side_effect = side_effect

    config = ModelSwarmConfig(retry_policy_max_retries=1, fallback_policy_enabled=False)
    subtask = ModelSubtask(subtask_id="s1", description="flaky task")
    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(_make_request([subtask], {"s1": "ep1"}, [ep], config))

    assert result.dispatches[0].execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.dispatches[0].retry_count == 1
    assert call_count == 2


@pytest.mark.unit
def test_fallback_to_alternate_endpoint() -> None:
    ep1 = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    ep2 = ModelSwarmEndpoint(endpoint_id="ep2", base_url="http://ep2", model_id="m2")

    def side_effect(url: str, *, json: Any, timeout: float) -> MagicMock:
        if "ep1" in url:
            raise RuntimeError("ep1 down")
        return _make_response("fallback ok")

    client = MagicMock()
    client.post.side_effect = side_effect

    config = ModelSwarmConfig(retry_policy_max_retries=0, fallback_policy_enabled=True)
    subtask = ModelSubtask(subtask_id="s1", description="task")
    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(_make_request([subtask], {"s1": "ep1"}, [ep1, ep2], config))

    d = result.dispatches[0]
    assert d.execution_status == EnumExecutionStatus.SUCCEEDED
    assert d.fallback_endpoint_id == "ep2"
    assert d.response_text == "fallback ok"


@pytest.mark.unit
def test_all_subtasks_fail_returns_result_with_failures() -> None:
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    client = MagicMock()
    client.post.side_effect = RuntimeError("always fails")

    config = ModelSwarmConfig(retry_policy_max_retries=0, fallback_policy_enabled=False)
    subtasks = [
        ModelSubtask(subtask_id=f"s{i}", description=f"task{i}") for i in range(3)
    ]
    assignments = {s.subtask_id: "ep1" for s in subtasks}
    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(_make_request(subtasks, assignments, [ep], config))

    assert len(result.dispatches) == 3
    assert all(
        d.execution_status == EnumExecutionStatus.FAILED for d in result.dispatches
    )
    assert all(d.failure_reason == "always fails" for d in result.dispatches)
    assert result.wall_latency_ms >= 0


@pytest.mark.unit
def test_no_assignment_for_subtask_fails_gracefully() -> None:
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    subtask = ModelSubtask(subtask_id="s1", description="orphan task")
    handler = HandlerSwarmFanout(http_client=MagicMock())

    result = handler.handle(_make_request([subtask], {}, [ep]))

    d = result.dispatches[0]
    assert d.execution_status == EnumExecutionStatus.FAILED
    assert "no_endpoint_assigned" in d.failure_reason


@pytest.mark.unit
def test_multi_wave_three_levels() -> None:
    """A → B → C must produce three distinct waves."""
    ep = ModelSwarmEndpoint(endpoint_id="ep1", base_url="http://ep1", model_id="m1")
    client = _make_client("ok")

    s_a = ModelSubtask(subtask_id="a", description="a", depends_on=())
    s_b = ModelSubtask(subtask_id="b", description="b", depends_on=("a",))
    s_c = ModelSubtask(subtask_id="c", description="c", depends_on=("b",))

    handler = HandlerSwarmFanout(http_client=client)
    result = handler.handle(
        _make_request([s_a, s_b, s_c], {"a": "ep1", "b": "ep1", "c": "ep1"}, [ep])
    )

    wave_by_id = {d.subtask_id: d.wave for d in result.dispatches}
    assert wave_by_id["a"] == 0
    assert wave_by_id["b"] == 1
    assert wave_by_id["c"] == 2
