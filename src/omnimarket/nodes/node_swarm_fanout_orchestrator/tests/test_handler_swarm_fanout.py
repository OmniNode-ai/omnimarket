# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmFanout orchestrator — FSM transitions, command building, result collection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_swarm_fanout_orchestrator.handlers.handler_swarm_fanout import (
    HandlerSwarmFanout,
    _compute_waves,
    _correlation_id,
    _owns_correlation_id,
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
    ep_id: str = "ep1", ref: str = "LLM_LOCAL_PRIMARY_URL"
) -> ModelSwarmEndpoint:
    return ModelSwarmEndpoint(
        endpoint_id=ep_id,
        base_url="http://192.168.86.201:8000/v1",  # onex-allow-internal-ip OMN-12118 reason="test fixture uses lab endpoint to exercise real endpoint_ref wiring"
        model_id="Qwen3.6-35B-A3B",
        endpoint_ref=ref,
    )


def _make_request(
    subtasks: list[ModelSubtask],
    assignments: dict[str, str],
    endpoints: list[ModelSwarmEndpoint],
    config: ModelSwarmConfig | None = None,
    run_id: str = "run-abc",
) -> ModelSwarmFanoutRequest:
    return ModelSwarmFanoutRequest(
        subtasks=tuple(subtasks),
        assignments=assignments,
        endpoints=tuple(endpoints),
        config=config or ModelSwarmConfig(),
        correlation_id="corr-123",
        run_id=run_id,
    )


def _completion_event(
    run_id: str, subtask_id: str, success: bool = True
) -> dict[str, object]:
    return {
        "correlation_id": _correlation_id(run_id, subtask_id),
        "success": success,
        "model_id": "Qwen3.6-35B-A3B",
        "latency_ms": 50,
        "_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
    }


def _failure_event(run_id: str, subtask_id: str) -> dict[str, object]:
    return {
        "correlation_id": _correlation_id(run_id, subtask_id),
        "success": False,
        "model_id": "",
        "latency_ms": 10,
        "failure_class": "all_tiers_exhausted",
        "_topic": "onex.evt.omnimarket.delegation-all-tiers-failed.v1",
    }


@pytest.mark.unit
def test_correlation_id_format() -> None:
    assert _correlation_id("run-1", "s1") == "run-1-s1"


@pytest.mark.unit
def test_owns_correlation_id_true() -> None:
    assert _owns_correlation_id("run-1", "run-1-s1") is True


@pytest.mark.unit
def test_owns_correlation_id_false_unrelated() -> None:
    assert _owns_correlation_id("run-1", "run-2-s1") is False


@pytest.mark.unit
def test_no_publisher_uses_stub_and_returns_result() -> None:
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="task")
    req = _make_request([s], {"s1": "ep1"}, [ep])
    # No queue publisher/subscriber injected — stub no-ops are used; subtask has
    # no delegation target so it lands in FAILED (no_endpoint_assigned) path.
    result = HandlerSwarmFanout().handle(req)
    assert result.run_id == req.run_id


@pytest.mark.unit
def test_single_subtask_success() -> None:
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="do the thing")
    pub = _make_publisher()
    sub = _make_subscriber([_completion_event("run-abc", "s1")])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    result = handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    assert len(result.dispatches) == 1
    d = result.dispatches[0]
    assert d.subtask_id == "s1"
    assert d.execution_status == EnumExecutionStatus.SUCCEEDED
    assert result.completed_count == 1
    assert result.failed_count == 0
    assert result.degraded is False


@pytest.mark.unit
def test_publishes_delegation_execute_command() -> None:
    """Handler publishes one delegation-execute command per subtask."""
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="some prompt")
    pub = _make_publisher()
    sub = _make_subscriber([_completion_event("run-abc", "s1")])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    pub.publish.assert_called_once()
    topic, payload = pub.publish.call_args.args
    assert "delegation-execute" in topic
    assert payload["endpoint_ref"] == "LLM_LOCAL_PRIMARY_URL"
    assert payload["model_id"] == "Qwen3.6-35B-A3B"
    assert payload["correlation_id"] == "run-abc-s1"
    assert payload["causation_id"] == "run-abc"
    assert payload["prompt"] == "some prompt"
    assert "prompt_hash" in payload


@pytest.mark.unit
def test_no_http_import_in_handler() -> None:
    """Verify the orchestrator handler contains no httpx import."""
    import ast
    from pathlib import Path

    handler_src = (
        Path(__file__).parent.parent / "handlers" / "handler_swarm_fanout.py"
    ).read_text()
    tree = ast.parse(handler_src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "httpx" not in alias.name, (
                        "httpx import found in orchestrator"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "httpx" not in node.module, "httpx import found in orchestrator"


@pytest.mark.unit
def test_wave_ordering_wave0_dispatched_before_wave1() -> None:
    """Wave 0 subtasks are published before wave 1 subtasks."""
    ep = _make_endpoint()
    s1 = ModelSubtask(subtask_id="s1", description="first", depends_on=())
    s2 = ModelSubtask(subtask_id="s2", description="second", depends_on=("s1",))

    published_order: list[str] = []
    pub = MagicMock()

    def capture_publish(topic: str, payload: dict[str, object]) -> None:
        published_order.append(str(payload.get("task_id", "")))

    pub.publish = MagicMock(side_effect=capture_publish)

    sub = _make_subscriber(
        [
            _completion_event("run-abc", "s1"),
            _completion_event("run-abc", "s2"),
        ]
    )
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep]))

    assert len(result.dispatches) == 2
    assert all(
        d.execution_status == EnumExecutionStatus.SUCCEEDED for d in result.dispatches
    )
    wave_by_id = {d.subtask_id: d.wave for d in result.dispatches}
    assert wave_by_id["s1"] < wave_by_id["s2"]


@pytest.mark.unit
def test_dependency_failed_skips_dependent() -> None:
    """If s1 fails, s2 (which depends on s1) is skipped without being published."""
    ep = _make_endpoint()
    s1 = ModelSubtask(subtask_id="s1", description="root", depends_on=())
    s2 = ModelSubtask(subtask_id="s2", description="child", depends_on=("s1",))

    pub = _make_publisher()
    sub = _make_subscriber([_failure_event("run-abc", "s1")])
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
def test_timeout_when_no_completion_event() -> None:
    """If no terminal event arrives for a subtask, it is marked TIMEOUT."""
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="slow task")
    pub = _make_publisher()
    sub = _make_subscriber([])
    config = ModelSwarmConfig(per_endpoint_timeout_seconds=1)
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    result = handler.handle(_make_request([s], {"s1": "ep1"}, [ep], config))

    d = result.dispatches[0]
    assert d.execution_status == EnumExecutionStatus.TIMEOUT
    assert "no_terminal_event" in d.failure_reason


@pytest.mark.unit
def test_no_endpoint_assigned_fails_immediately() -> None:
    """If no endpoint_id in assignments, dispatch fails without publishing."""
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="orphan task")
    pub = _make_publisher()
    sub = _make_subscriber([])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    result = handler.handle(_make_request([s], {}, [ep]))

    d = result.dispatches[0]
    assert d.execution_status == EnumExecutionStatus.FAILED
    assert "no_endpoint" in d.failure_reason
    pub.publish.assert_not_called()


@pytest.mark.unit
def test_endpoint_missing_endpoint_ref_fails_immediately() -> None:
    """Endpoint without endpoint_ref cannot be dispatched."""
    ep = ModelSwarmEndpoint(
        endpoint_id="ep1",
        base_url="http://192.168.86.201:8000/v1",  # onex-allow-internal-ip OMN-12118 reason="test fixture uses lab endpoint to exercise real endpoint_ref wiring"
        model_id="m1",
        endpoint_ref="",
    )
    s = ModelSubtask(subtask_id="s1", description="task")
    pub = _make_publisher()
    sub = _make_subscriber([])
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    result = handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    d = result.dispatches[0]
    assert d.execution_status == EnumExecutionStatus.FAILED
    pub.publish.assert_not_called()


@pytest.mark.unit
def test_unrelated_correlation_id_filtered_out() -> None:
    """Events from a different run_id are not attributed to our subtasks."""
    ep = _make_endpoint()
    s = ModelSubtask(subtask_id="s1", description="task")
    pub = _make_publisher()
    sub = _make_subscriber(
        [
            {
                "correlation_id": "other-run-s1",
                "success": True,
                "model_id": "m1",
                "latency_ms": 10,
                "_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
            }
        ]
    )
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)

    result = handler.handle(_make_request([s], {"s1": "ep1"}, [ep]))

    assert result.dispatches[0].execution_status == EnumExecutionStatus.TIMEOUT


@pytest.mark.unit
def test_three_wave_chain() -> None:
    """A → B → C produces three distinct waves."""
    ep = _make_endpoint()
    s_a = ModelSubtask(subtask_id="a", description="a", depends_on=())
    s_b = ModelSubtask(subtask_id="b", description="b", depends_on=("a",))
    s_c = ModelSubtask(subtask_id="c", description="c", depends_on=("b",))

    pub = _make_publisher()
    sub = _make_subscriber(
        [
            _completion_event("run-abc", "a"),
            _completion_event("run-abc", "b"),
            _completion_event("run-abc", "c"),
        ]
    )
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(
        _make_request([s_a, s_b, s_c], {"a": "ep1", "b": "ep1", "c": "ep1"}, [ep])
    )

    wave_by_id = {d.subtask_id: d.wave for d in result.dispatches}
    assert wave_by_id["a"] == 0
    assert wave_by_id["b"] == 1
    assert wave_by_id["c"] == 2


@pytest.mark.unit
def test_degraded_flag_set_when_any_failure() -> None:
    ep = _make_endpoint()
    s1 = ModelSubtask(subtask_id="s1", description="ok")
    s2 = ModelSubtask(subtask_id="s2", description="fail")

    pub = _make_publisher()
    sub = _make_subscriber(
        [
            _completion_event("run-abc", "s1", success=True),
            _failure_event("run-abc", "s2"),
        ]
    )
    handler = HandlerSwarmFanout(queue_publisher=pub, queue_subscriber=sub)
    result = handler.handle(_make_request([s1, s2], {"s1": "ep1", "s2": "ep1"}, [ep]))

    assert result.degraded is True
    assert result.completed_count == 1
    assert result.failed_count == 1


@pytest.mark.unit
def test_compute_waves_empty() -> None:
    assert _compute_waves(()) == []


@pytest.mark.unit
def test_compute_waves_no_deps() -> None:
    subtasks = tuple(
        ModelSubtask(subtask_id=f"s{i}", description="x") for i in range(3)
    )
    waves = _compute_waves(subtasks)
    assert len(waves) == 1
    assert len(waves[0]) == 3


@pytest.mark.unit
def test_compute_waves_stable_sort_within_wave() -> None:
    """Subtasks within a wave are sorted by subtask_id for determinism."""
    s_b = ModelSubtask(subtask_id="b", description="b")
    s_a = ModelSubtask(subtask_id="a", description="a")
    waves = _compute_waves((s_b, s_a))
    assert [s.subtask_id for s in waves[0]] == ["a", "b"]
