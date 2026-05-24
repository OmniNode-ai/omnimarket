# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmAggregator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_swarm_aggregator_compute.handlers.handler_swarm_aggregator import (
    HandlerSwarmAggregator,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.enums import (
    EnumAggregationMode,
    EnumDecompositionStatus,
    EnumSubtaskCategory,
    EnumSubtaskStatus,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_decomposition import (
    ModelDecomposition,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_request import (
    ModelSwarmAggregateRequest,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
    ModelSwarmDispatchResult,
)


def _make_subtask(subtask_id: str, depends_on: tuple[str, ...] = ()) -> ModelSubtask:
    return ModelSubtask(
        subtask_id=subtask_id,
        description=f"desc {subtask_id}",
        model_affinity="general",
        depends_on=depends_on,
        category=EnumSubtaskCategory.GENERAL,
    )


def _make_decomposition(*subtask_ids: str) -> ModelDecomposition:
    return ModelDecomposition(
        original_task="test task",
        original_task_hash="abc123",
        subtasks=tuple(_make_subtask(sid) for sid in subtask_ids),
        decomposition_model="test-model",
        decomposition_endpoint_id="ep1",
        decomposition_latency_ms=100,
        decomposition_status=EnumDecompositionStatus.SUCCEEDED,
        decomposition_run_id="run-1",
        correlation_id="corr-1",
    )


def _make_dispatch(
    subtask_id: str,
    status: EnumSubtaskStatus,
    response_text: str = "",
    wave: int = 0,
) -> ModelSwarmDispatch:
    result = (
        ModelSwarmDispatchResult(response_text=response_text) if response_text else None
    )
    return ModelSwarmDispatch(
        subtask_id=subtask_id,
        endpoint_id="ep1",
        model_id="model1",
        base_url="http://localhost:8000",
        status=status,
        result=result,
        wave=wave,
    )


@pytest.mark.unit
class TestConcatenationMode:
    def test_all_succeeded(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1", "s2", "s3")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "output one", wave=0),
            _make_dispatch("s2", EnumSubtaskStatus.SUCCEEDED, "output two", wave=0),
            _make_dispatch("s3", EnumSubtaskStatus.SUCCEEDED, "output three", wave=1),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="concatenation",
            correlation_id="corr-1",
        )
        result = handler.run(request)

        assert result.aggregation_mode == EnumAggregationMode.CONCATENATION
        assert "output one" in result.aggregated_output
        assert "output two" in result.aggregated_output
        assert "output three" in result.aggregated_output
        assert result.failed_subtasks == ()
        assert result.skipped_subtasks == ()
        assert result.degraded_reason == ""

    def test_subtask_ordering_respects_decomposition_order(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("first", "second", "third")
        # Dispatches arrive out of order
        dispatches = (
            _make_dispatch("third", EnumSubtaskStatus.SUCCEEDED, "C", wave=0),
            _make_dispatch("first", EnumSubtaskStatus.SUCCEEDED, "A", wave=0),
            _make_dispatch("second", EnumSubtaskStatus.SUCCEEDED, "B", wave=0),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="concatenation",
            correlation_id="corr-2",
        )
        result = handler.run(request)

        pos_a = result.aggregated_output.index("A")
        pos_b = result.aggregated_output.index("B")
        pos_c = result.aggregated_output.index("C")
        assert pos_a < pos_b < pos_c

    def test_partial_failures_produce_degraded_output(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1", "s2", "s3")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "good output", wave=0),
            _make_dispatch("s2", EnumSubtaskStatus.FAILED, wave=0),
            _make_dispatch("s3", EnumSubtaskStatus.TIMEOUT, wave=1),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="concatenation",
            correlation_id="corr-3",
        )
        result = handler.run(request)

        assert "good output" in result.aggregated_output
        assert "s2" in result.failed_subtasks
        assert "s3" in result.failed_subtasks
        assert result.degraded_reason != ""

    def test_all_failed_produces_empty_output(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1", "s2")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.FAILED, wave=0),
            _make_dispatch("s2", EnumSubtaskStatus.CONTEXT_WINDOW_EXCEEDED, wave=0),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="concatenation",
            correlation_id="corr-4",
        )
        result = handler.run(request)

        assert result.aggregated_output == ""
        assert set(result.failed_subtasks) == {"s1", "s2"}
        assert result.degraded_reason != ""

    def test_skipped_subtasks_recorded(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1", "s2")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "output", wave=0),
            _make_dispatch("s2", EnumSubtaskStatus.SKIPPED_DEPENDENCY_FAILED, wave=0),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="concatenation",
            correlation_id="corr-5",
        )
        result = handler.run(request)

        assert "s2" in result.skipped_subtasks
        assert result.failed_subtasks == ()
        assert result.degraded_reason != ""

    def test_zero_dispatches_returns_empty(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition()
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=(),
            mode="concatenation",
            correlation_id="corr-6",
        )
        result = handler.run(request)

        assert result.aggregated_output == ""
        assert result.failed_subtasks == ()
        assert result.skipped_subtasks == ()
        assert result.degraded_reason == ""

    def test_wave_ordering_secondary_to_decomposition_order(self) -> None:
        handler = HandlerSwarmAggregator()
        # s1 wave=1, s2 wave=0 — wave should be secondary to decomposition index
        decomp = _make_decomposition("s1", "s2")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "first-out", wave=1),
            _make_dispatch("s2", EnumSubtaskStatus.SUCCEEDED, "second-out", wave=0),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="concatenation",
            correlation_id="corr-7",
        )
        result = handler.run(request)

        # wave is the primary sort key, so s2 (wave=0) comes before s1 (wave=1)
        pos_first = result.aggregated_output.index("first-out")
        pos_second = result.aggregated_output.index("second-out")
        assert pos_second < pos_first


@pytest.mark.unit
class TestSynthesisMode:
    def test_uses_synthesis_output_directly(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1", "s2")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "raw output", wave=0),
            _make_dispatch("s2", EnumSubtaskStatus.SUCCEEDED, "raw output 2", wave=0),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="synthesis",
            synthesis_output="synthesized answer",
            synthesis_model_id="qwen3-30b",
            synthesis_input_hash="sha256abc",
            correlation_id="corr-8",
        )
        result = handler.run(request)

        assert result.aggregated_output == "synthesized answer"
        assert result.aggregation_mode == EnumAggregationMode.SYNTHESIS
        assert result.synthesis_model_id == "qwen3-30b"
        assert result.synthesis_input_hash == "sha256abc"

    def test_synthesis_falls_back_to_concatenation_when_no_synthesis_output(
        self,
    ) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "inline text", wave=0),
        )
        # synthesis mode but synthesis_output is None — falls back to concatenation
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="synthesis",
            synthesis_output=None,
            correlation_id="corr-9",
        )
        result = handler.run(request)

        assert result.aggregation_mode == EnumAggregationMode.CONCATENATION
        assert "inline text" in result.aggregated_output

    def test_synthesis_records_failed_subtasks(self) -> None:
        handler = HandlerSwarmAggregator()
        decomp = _make_decomposition("s1", "s2")
        dispatches = (
            _make_dispatch("s1", EnumSubtaskStatus.SUCCEEDED, "ok", wave=0),
            _make_dispatch("s2", EnumSubtaskStatus.FAILED, wave=0),
        )
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=dispatches,
            mode="synthesis",
            synthesis_output="synth out",
            correlation_id="corr-10",
        )
        result = handler.run(request)

        assert result.aggregated_output == "synth out"
        assert "s2" in result.failed_subtasks


@pytest.mark.unit
class TestModelValidation:
    def test_request_model_is_frozen(self) -> None:
        decomp = _make_decomposition("s1")
        request = ModelSwarmAggregateRequest(
            decomposition=decomp,
            dispatches=(),
            correlation_id="corr-x",
        )
        with pytest.raises(ValidationError):
            request.mode = "other"  # type: ignore[misc]

    def test_result_model_is_frozen(self) -> None:
        from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_result import (
            ModelSwarmAggregateResult,
        )

        result = ModelSwarmAggregateResult(
            aggregated_output="out",
            aggregation_mode="concatenation",
        )
        with pytest.raises(ValidationError):
            result.aggregated_output = "changed"  # type: ignore[misc]

    def test_request_rejects_extra_fields(self) -> None:
        decomp = _make_decomposition("s1")
        with pytest.raises(ValidationError):
            ModelSwarmAggregateRequest(  # type: ignore[call-arg]
                decomposition=decomp,
                dispatches=(),
                correlation_id="corr-x",
                unknown_extra_field="bad",
            )
