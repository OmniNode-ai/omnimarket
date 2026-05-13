# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_overseer_benchmarker (OMN-8055).

Covers the three mandatory test cases from the ticket spec:
  - test_benchmark_run_appends_to_ledger
  - test_benchmark_uses_existing_eval_run_model
  - test_benchmark_does_not_mutate_active_state
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_llm_eval_harness.handlers.handler_llm_eval_harness import (
    EnumLlmEvalTaskType,
    FakeLlmClient,
    ModelLlmEvalSample,
    ModelLlmEvalTask,
    NodeLlmEvalHarness,
)
from omnimarket.nodes.node_overseer_benchmarker.handlers.handler_overseer_benchmarker import (
    BenchmarkRequest,
    BenchmarkResult,
    ModelScorecardRow,
    NodeOverseerBenchmarker,
    _append_rows,
    _sample_to_row,
)

LEDGER_FILENAME = "overseer_performance_ledger.jsonl"


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a fresh ONEX_STATE_ROOT for each test."""
    monkeypatch.setenv("ONEX_STATE_ROOT", str(tmp_path))
    return tmp_path


class _MinimalCorpusHarness(NodeLlmEvalHarness):
    """Harness pre-loaded with a one-task corpus to keep tests fast."""

    _CORPUS = (
        ModelLlmEvalTask(
            task_id="classify_yes",
            task_type=EnumLlmEvalTaskType.CLASSIFICATION,
            prompt="classify this",
            expected_substrings=("YES",),
        ),
    )

    def handle(self, request):  # type: ignore[override]
        patched = request.model_copy(update={"corpus": self._CORPUS})
        return super().handle(patched)


def _make_minimal_benchmarker(client: object | None = None) -> NodeOverseerBenchmarker:
    """Return a benchmarker with a one-task harness injected via DI."""
    harness = _MinimalCorpusHarness(
        client=client or FakeLlmClient(responses={"classify": "YES"})
    )
    return NodeOverseerBenchmarker(harness=harness)


# Keep subclass for tests that need to call handle() directly on a typed instance.
class _MinimalCorpusBenchmarker(NodeOverseerBenchmarker):
    """Benchmarker that uses _MinimalCorpusHarness injected via DI."""

    def __init__(self, client: object | None = None) -> None:
        harness = _MinimalCorpusHarness(
            client=client or FakeLlmClient(responses={"classify": "YES"})
        )
        super().__init__(harness=harness)


@pytest.mark.unit
class TestBenchmarkRunAppendsToLedger:
    """test_benchmark_run_appends_to_ledger — ledger file gains one row per sample."""

    def test_benchmark_run_appends_to_ledger(self, state_root: Path) -> None:
        benchmarker = _MinimalCorpusBenchmarker(
            client=FakeLlmClient(responses={"classify": "YES"})
        )
        request = BenchmarkRequest(run_id="run-001", models=["qwen3-coder-30b"])

        result = benchmarker.handle(request)

        assert result.run_id == "run-001"
        assert result.rows_appended == 1
        assert result.status == "clean"
        assert result.dry_run is False

        ledger = state_root / LEDGER_FILENAME
        assert ledger.exists(), "ledger file must be created"
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 1

        row = json.loads(lines[0])
        assert row["run_id"] == "run-001"
        assert row["model_key"] == "qwen3-coder-30b"
        assert row["task_id"] == "classify_yes"
        assert row["task_type"] == "classification"
        assert "score" in row
        assert "recorded_at" in row

    def test_multiple_runs_append_cumulatively(self, state_root: Path) -> None:
        benchmarker = _MinimalCorpusBenchmarker(
            client=FakeLlmClient(responses={"classify": "YES"})
        )

        benchmarker.handle(BenchmarkRequest(run_id="run-001", models=["m1"]))
        benchmarker.handle(BenchmarkRequest(run_id="run-002", models=["m2"]))

        ledger = state_root / LEDGER_FILENAME
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 2
        run_ids = {json.loads(line)["run_id"] for line in lines}
        assert run_ids == {"run-001", "run-002"}

    def test_dry_run_does_not_write_ledger(self, state_root: Path) -> None:
        benchmarker = _MinimalCorpusBenchmarker()
        request = BenchmarkRequest(run_id="dry-001", models=["m1"], dry_run=True)

        result = benchmarker.handle(request)

        assert result.dry_run is True
        assert result.rows_appended == 0
        assert result.status == "dry_run"
        ledger = state_root / LEDGER_FILENAME
        assert not ledger.exists(), "dry_run must not create the ledger file"


@pytest.mark.unit
class TestBenchmarkUsesExistingEvalRunModel:
    """test_benchmark_uses_existing_eval_run_model — reuses NodeLlmEvalHarness models."""

    def test_benchmark_uses_existing_eval_run_model(self, state_root: Path) -> None:
        client = FakeLlmClient(responses={"classify": "YES"})
        benchmarker = _MinimalCorpusBenchmarker(client=client)
        request = BenchmarkRequest(run_id="run-eval-model", models=["model-a"])

        result = benchmarker.handle(request)

        assert result.rows_appended >= 1
        ledger = state_root / LEDGER_FILENAME
        row_data = json.loads(ledger.read_text().splitlines()[0])
        # Confirms the scorecard row carries fields produced by ModelLlmEvalSample
        assert "score" in row_data
        assert "latency_ms" in row_data
        assert "ruff_pass" in row_data
        assert "mypy_pass" in row_data
        assert "substring_hits" in row_data
        assert "error" in row_data

    def test_sample_to_row_preserves_all_eval_fields(self) -> None:
        sample = ModelLlmEvalSample(
            model_key="m1",
            task_id="t1",
            task_type=EnumLlmEvalTaskType.CLASSIFICATION,
            score=0.75,
            latency_ms=123,
            output_chars=42,
            ruff_pass=False,
            mypy_pass=False,
            substring_hits=1,
            error="",
        )
        row = _sample_to_row("run-x", sample)

        assert isinstance(row, ModelScorecardRow)
        assert row.run_id == "run-x"
        assert row.model_key == "m1"
        assert row.task_id == "t1"
        assert row.task_type == "classification"
        assert row.score == 0.75
        assert row.latency_ms == 123
        assert row.substring_hits == 1
        assert row.error == ""
        assert row.recorded_at  # non-empty ISO string

    def test_harness_is_composable_as_dependency(self, state_root: Path) -> None:
        """NodeOverseerBenchmarker accepts any ProtocolLlmClient — not hardwired."""
        client = FakeLlmClient(responses={"classify": "YES"})
        benchmarker = _MinimalCorpusBenchmarker(client=client)
        result = benchmarker.handle(BenchmarkRequest(run_id="r1", models=["m1"]))
        assert isinstance(result, BenchmarkResult)

    def test_harness_injected_directly_via_di(self, state_root: Path) -> None:
        """NodeLlmEvalHarness can be injected directly; client param is ignored."""
        harness = _MinimalCorpusHarness(
            client=FakeLlmClient(responses={"classify": "YES"})
        )
        benchmarker = NodeOverseerBenchmarker(harness=harness)
        result = benchmarker.handle(BenchmarkRequest(run_id="di-001", models=["m1"]))
        assert isinstance(result, BenchmarkResult)
        assert result.rows_appended == 1
        assert result.run_id == "di-001"

    def test_harness_param_takes_precedence_over_client(self, state_root: Path) -> None:
        """When harness is injected, the client param is not used to build a new harness."""
        sentinel_calls: list[str] = []

        class _TrackingHarness(NodeLlmEvalHarness):
            def handle(self, request):  # type: ignore[override]
                sentinel_calls.append("harness_called")
                patched = request.model_copy(
                    update={
                        "corpus": (
                            ModelLlmEvalTask(
                                task_id="t1",
                                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                                prompt="x",
                                expected_substrings=("YES",),
                            ),
                        )
                    }
                )
                return super().handle(patched)

        injected = _TrackingHarness(client=FakeLlmClient(responses={"x": "YES"}))
        benchmarker = NodeOverseerBenchmarker(
            harness=injected,
            client=FakeLlmClient(),  # should be ignored
        )
        benchmarker.handle(BenchmarkRequest(run_id="r1", models=["m1"]))
        assert sentinel_calls == ["harness_called"]


@pytest.mark.unit
class TestBenchmarkDoesNotMutateActiveState:
    """test_benchmark_does_not_mutate_active_state — ledger is append-only."""

    def test_benchmark_does_not_mutate_active_state(self, state_root: Path) -> None:
        sentinel_file = state_root / "active_run_state.json"
        sentinel_content = '{"session_id": "abc", "status": "running"}'
        sentinel_file.write_text(sentinel_content)

        benchmarker = _MinimalCorpusBenchmarker(
            client=FakeLlmClient(responses={"classify": "YES"})
        )
        benchmarker.handle(BenchmarkRequest(run_id="run-safe", models=["m1"]))

        # Active state file must be completely untouched
        assert sentinel_file.read_text() == sentinel_content

    def test_ledger_rows_are_independent_of_active_session(
        self, state_root: Path
    ) -> None:
        active = state_root / "active_run_state.json"
        active.write_text('{"session": "live"}')

        benchmarker = _MinimalCorpusBenchmarker(
            client=FakeLlmClient(responses={"classify": "YES"})
        )
        benchmarker.handle(BenchmarkRequest(run_id="r1", models=["m1"]))
        benchmarker.handle(BenchmarkRequest(run_id="r2", models=["m2"]))

        # Active state still untouched after two benchmark runs
        assert active.read_text() == '{"session": "live"}'
        # Only the ledger file was created/appended
        files_created = {f.name for f in state_root.iterdir()}
        assert "overseer_performance_ledger.jsonl" in files_created
        assert "active_run_state.json" in files_created

    def test_append_rows_is_safe_on_empty_ledger(self, state_root: Path) -> None:
        ledger = state_root / LEDGER_FILENAME
        sample = ModelLlmEvalSample(
            model_key="m",
            task_id="t",
            task_type=EnumLlmEvalTaskType.CLASSIFICATION,
            score=1.0,
            latency_ms=10,
            output_chars=5,
        )
        rows = [_sample_to_row("r0", sample)]
        _append_rows(ledger, rows)

        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["run_id"] == "r0"
