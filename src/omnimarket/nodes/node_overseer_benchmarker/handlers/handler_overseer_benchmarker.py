# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeOverseerBenchmarker — continuous benchmarking harness for overseer.

Wraps NodeLlmEvalHarness, converts samples to scorecard rows, and appends
them to overseer_performance_ledger.jsonl. Never reads or mutates active
run state — append-only, isolated from any live overseer session.

ONEX node type: COMPUTE (nondeterministic — delegates to LLM eval harness).
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_llm_eval_harness.handlers.handler_llm_eval_harness import (
    FakeLlmClient,
    LlmEvalRequest,
    ModelLlmEvalSample,
    NodeLlmEvalHarness,
    ProtocolLlmClient,
)

LEDGER_FILENAME = "overseer_performance_ledger.jsonl"


def _ledger_path() -> Path:
    return Path(os.environ["ONEX_STATE_ROOT"]) / LEDGER_FILENAME


class ModelScorecardRow(BaseModel):
    """One row appended to overseer_performance_ledger.jsonl per eval sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    model_key: str
    task_id: str
    task_type: str
    score: float
    latency_ms: int
    ruff_pass: bool
    mypy_pass: bool
    substring_hits: int
    error: str
    recorded_at: str  # ISO-8601 UTC


class BenchmarkRequest(BaseModel):
    """Input for the overseer benchmarker handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Caller-assigned ID for this benchmark run")
    models: list[str] = Field(default_factory=list)
    max_tasks_per_type: int = 5
    dry_run: bool = False


class BenchmarkResult(BaseModel):
    """Output of the overseer benchmarker handler."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    rows_appended: int
    status: str  # clean | partial | error | dry_run
    dry_run: bool = False


def _sample_to_row(run_id: str, sample: ModelLlmEvalSample) -> ModelScorecardRow:
    return ModelScorecardRow(
        run_id=run_id,
        model_key=sample.model_key,
        task_id=sample.task_id,
        task_type=sample.task_type.value,
        score=sample.score,
        latency_ms=sample.latency_ms,
        ruff_pass=sample.ruff_pass,
        mypy_pass=sample.mypy_pass,
        substring_hits=sample.substring_hits,
        error=sample.error,
        recorded_at=datetime.now(UTC).isoformat(),
    )


def _append_rows(ledger: Path, rows: list[ModelScorecardRow]) -> None:
    """Append rows to the JSONL ledger with exclusive lock to prevent interleaving."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            for row in rows:
                fh.write(json.dumps(row.model_dump()) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class NodeOverseerBenchmarker:
    """Run LLM eval and append scorecard rows to overseer_performance_ledger.

    Uses NodeLlmEvalHarness for the actual eval — all scoring logic lives
    there. This node is responsible only for persisting results to the ledger
    and never touching any active overseer run state.
    """

    def __init__(self, client: ProtocolLlmClient | None = None) -> None:
        self._harness = NodeLlmEvalHarness(client=client or FakeLlmClient())

    def handle(self, request: BenchmarkRequest) -> BenchmarkResult:
        if request.dry_run:
            return BenchmarkResult(
                run_id=request.run_id,
                rows_appended=0,
                status="dry_run",
                dry_run=True,
            )

        eval_request = LlmEvalRequest(
            models=request.models,
            max_tasks_per_type=request.max_tasks_per_type,
            dry_run=False,
        )
        eval_result = self._harness.handle(eval_request)

        rows = [_sample_to_row(request.run_id, s) for s in eval_result.samples]
        if rows:
            _append_rows(_ledger_path(), rows)

        return BenchmarkResult(
            run_id=request.run_id,
            rows_appended=len(rows),
            status=eval_result.status,
            dry_run=False,
        )
