# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI injection-path tests for OMN-10873.

Verifies that NodeOverseerBenchmarker accepts a ProtocolLlmEvalHarness stub
and routes all calls through it instead of constructing a concrete harness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_llm_eval_harness.handlers.handler_llm_eval_harness import (
    EnumLlmEvalTaskType,
    FakeLlmClient,
    LlmEvalRequest,
    LlmEvalResult,
    ModelLlmEvalSample,
    NodeLlmEvalHarness,
    ProtocolLlmEvalHarness,
)
from omnimarket.nodes.node_overseer_benchmarker.handlers.handler_overseer_benchmarker import (
    BenchmarkRequest,
    BenchmarkResult,
    NodeOverseerBenchmarker,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ONEX_STATE_ROOT", str(tmp_path))
    return tmp_path


class _StubHarness:
    """Minimal ProtocolLlmEvalHarness stub — returns one canned sample."""

    def __init__(self) -> None:
        self.handle_calls: list[LlmEvalRequest] = []

    def handle(self, request: LlmEvalRequest) -> LlmEvalResult:
        self.handle_calls.append(request)
        return LlmEvalResult(
            samples=[
                ModelLlmEvalSample(
                    model_key=request.models[0] if request.models else "stub-model",
                    task_id="stub-task",
                    task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                    score=1.0,
                    latency_ms=1,
                    output_chars=3,
                )
            ],
            models_benchmarked=1,
            tasks_run=1,
            status="clean",
        )


@pytest.mark.unit
class TestOverseerBenchmarkerDiInjection:
    """OMN-10873: benchmarker accepts ProtocolLlmEvalHarness via DI."""

    def test_protocol_is_runtime_checkable(self) -> None:
        stub = _StubHarness()
        assert isinstance(stub, ProtocolLlmEvalHarness)

    def test_concrete_harness_satisfies_protocol(self) -> None:
        harness = NodeLlmEvalHarness()
        assert isinstance(harness, ProtocolLlmEvalHarness)

    def test_injected_harness_is_used(self, state_root: Path) -> None:
        stub = _StubHarness()
        benchmarker = NodeOverseerBenchmarker(harness=stub)
        request = BenchmarkRequest(run_id="di-test", models=["test-model"])

        result = benchmarker.handle(request)

        assert len(stub.handle_calls) == 1
        assert stub.handle_calls[0].models == ["test-model"]
        assert result.run_id == "di-test"
        assert result.rows_appended == 1
        assert isinstance(result, BenchmarkResult)

    def test_harness_param_takes_precedence_over_client(self, state_root: Path) -> None:
        """When harness is injected, client param is not used to build a new harness."""
        stub = _StubHarness()
        benchmarker = NodeOverseerBenchmarker(
            harness=stub,
            client=FakeLlmClient(),  # should be ignored
        )
        benchmarker.handle(BenchmarkRequest(run_id="r1", models=["m1"]))
        assert len(stub.handle_calls) == 1

    def test_default_construction_uses_node_llm_eval_harness(
        self, state_root: Path
    ) -> None:
        """No harness injected — concrete NodeLlmEvalHarness is built."""
        benchmarker = NodeOverseerBenchmarker(client=FakeLlmClient())
        assert isinstance(benchmarker._harness, NodeLlmEvalHarness)

    def test_dry_run_bypasses_harness(self, state_root: Path) -> None:
        stub = _StubHarness()
        benchmarker = NodeOverseerBenchmarker(harness=stub)
        result = benchmarker.handle(
            BenchmarkRequest(run_id="dry", models=["m1"], dry_run=True)
        )

        assert result.dry_run is True
        assert result.rows_appended == 0
        assert len(stub.handle_calls) == 0
