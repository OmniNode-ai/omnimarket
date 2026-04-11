# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeLlmEvalHarness — Benchmark LLM output quality per model and task type.

Runs a fixed corpus of reference tasks through each target model and scores
the outputs with deterministic checks (ruff pass, mypy pass, length bounds,
substring match). Results feed the delegation router's scoring system,
replacing heuristic seed values once enough samples are collected.

ONEX node type: COMPUTE (nondeterministic — LLM calls).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from statistics import mean
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumLlmEvalTaskType(StrEnum):
    """Eval dimensions measured per model."""

    CODE_GENERATION = "code_generation"
    CLASSIFICATION = "classification"
    CONTRACT_YAML = "contract_yaml"
    TEST_GENERATION = "test_generation"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelLlmEvalTask(BaseModel):
    """A single reference task in the eval corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_type: EnumLlmEvalTaskType
    prompt: str
    # For substring-match scoring (classification, contract_yaml):
    expected_substrings: tuple[str, ...] = Field(default_factory=tuple)
    # For code tasks: language of the expected output (for syntax scoring)
    language: str = "python"


class ModelLlmEvalSample(BaseModel):
    """A single (model, task) evaluation result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str
    task_id: str
    task_type: EnumLlmEvalTaskType
    score: float  # 0.0 - 1.0
    latency_ms: int
    output_chars: int
    ruff_pass: bool = False
    mypy_pass: bool = False
    substring_hits: int = 0
    error: str = ""


class LlmEvalRequest(BaseModel):
    """Input for the eval harness handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: list[str] = Field(default_factory=list)
    task_types: list[EnumLlmEvalTaskType] = Field(default_factory=list)
    max_tasks_per_type: int = 5
    dry_run: bool = False
    # Optional corpus override for tests; None => use built-in default corpus.
    corpus: tuple[ModelLlmEvalTask, ...] | None = None


class LlmEvalResult(BaseModel):
    """Output of the eval harness handler."""

    model_config = ConfigDict(extra="forbid")

    samples: list[ModelLlmEvalSample] = Field(default_factory=list)
    models_benchmarked: int = 0
    tasks_run: int = 0
    status: str = "clean"  # clean | partial | error
    dry_run: bool = False

    @property
    def summary(self) -> dict[str, dict[str, float]]:
        """Aggregate to {model_key: {task_type: mean_score}}."""
        rollup: dict[str, dict[str, list[float]]] = {}
        for s in self.samples:
            rollup.setdefault(s.model_key, {}).setdefault(s.task_type.value, []).append(
                s.score
            )
        return {
            model_key: {tt: round(mean(scores), 3) for tt, scores in by_task.items()}
            for model_key, by_task in rollup.items()
        }


# ---------------------------------------------------------------------------
# LLM client protocol + fake
# ---------------------------------------------------------------------------


class ProtocolLlmClient(Protocol):
    """Minimal protocol for an LLM completion client.

    Real impls call LLM_CODER_URL, LLM_DEEPSEEK_R1_URL, frontier APIs, etc.
    The harness depends on this protocol, not a concrete adapter, so tests
    can inject deterministic fakes.
    """

    def complete(self, model_key: str, prompt: str) -> str: ...


class FakeLlmClient:
    """Deterministic fake for tests and dry-run mode.

    Returns canned outputs keyed by task prompt signature. Used by the
    golden chain test and by CLI `--dry-run` to smoke-test the harness
    without hitting real LLM endpoints.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}

    def complete(self, model_key: str, prompt: str) -> str:
        for key, value in self._responses.items():
            if key in prompt:
                return value
        return "def placeholder() -> None:\n    return None\n"


# ---------------------------------------------------------------------------
# Default task corpus
# ---------------------------------------------------------------------------


def _default_corpus() -> tuple[ModelLlmEvalTask, ...]:
    return (
        ModelLlmEvalTask(
            task_id="codegen_add",
            task_type=EnumLlmEvalTaskType.CODE_GENERATION,
            prompt=(
                "Write a Python function named `add` that takes two ints "
                "and returns their sum. Include type hints. Return only code."
            ),
        ),
        ModelLlmEvalTask(
            task_id="codegen_fibonacci",
            task_type=EnumLlmEvalTaskType.CODE_GENERATION,
            prompt=(
                "Write a Python function `fib(n: int) -> int` returning the "
                "nth Fibonacci number. Include type hints. Return only code."
            ),
        ),
        ModelLlmEvalTask(
            task_id="classify_sentiment",
            task_type=EnumLlmEvalTaskType.CLASSIFICATION,
            prompt=(
                "Classify the sentiment of this sentence as POSITIVE, "
                "NEGATIVE, or NEUTRAL: 'I love this product.' "
                "Respond with only the label."
            ),
            expected_substrings=("POSITIVE",),
        ),
        ModelLlmEvalTask(
            task_id="contract_minimal",
            task_type=EnumLlmEvalTaskType.CONTRACT_YAML,
            prompt=(
                "Write a minimal ONEX node contract.yaml with fields: "
                "name, node_type, contract_version. Return only YAML."
            ),
            expected_substrings=("name:", "node_type:", "contract_version:"),
        ),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_code_output(output: str, language: str) -> tuple[float, bool, bool]:
    """Score code output. Returns (score, ruff_pass, mypy_pass).

    Writes output to a temp file and runs ruff/mypy. Score is the fraction
    of checks that pass, weighted equally.
    """
    if language != "python" or not output.strip():
        return 0.0, False, False

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "candidate.py"
        path.write_text(output)

        ruff_pass = _run_tool(["ruff", "check", str(path)])
        mypy_pass = _run_tool(["mypy", "--ignore-missing-imports", str(path)])

    score = (int(ruff_pass) + int(mypy_pass)) / 2.0
    return score, ruff_pass, mypy_pass


def _run_tool(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _score_substring_output(
    output: str, expected: tuple[str, ...]
) -> tuple[float, int]:
    """Score by substring match. Returns (score, hit_count)."""
    if not expected:
        return 0.0, 0
    hits = sum(1 for sub in expected if sub in output)
    return hits / len(expected), hits


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeLlmEvalHarness:
    """Run eval tasks through each model and score the outputs.

    Depends on a ProtocolLlmClient for the actual LLM calls, so tests can
    inject a FakeLlmClient. Scoring is deterministic and offline.
    """

    def __init__(self, client: ProtocolLlmClient | None = None) -> None:
        self._client = client or FakeLlmClient()

    def handle(self, request: LlmEvalRequest) -> LlmEvalResult:
        if request.dry_run:
            return LlmEvalResult(
                samples=[],
                models_benchmarked=len(request.models),
                tasks_run=0,
                status="clean",
                dry_run=True,
            )

        corpus = request.corpus or _default_corpus()
        if request.task_types:
            wanted = set(request.task_types)
            corpus = tuple(t for t in corpus if t.task_type in wanted)

        samples: list[ModelLlmEvalSample] = []
        tasks_run = 0

        for model_key in request.models:
            per_type_counts: dict[EnumLlmEvalTaskType, int] = {}
            for task in corpus:
                count = per_type_counts.get(task.task_type, 0)
                if count >= request.max_tasks_per_type:
                    continue
                per_type_counts[task.task_type] = count + 1

                sample = self._run_one(model_key, task)
                samples.append(sample)
                tasks_run += 1

        status = "clean" if samples else "partial"
        if samples and all(s.error for s in samples):
            status = "error"

        return LlmEvalResult(
            samples=samples,
            models_benchmarked=len(request.models),
            tasks_run=tasks_run,
            status=status,
            dry_run=False,
        )

    def _run_one(self, model_key: str, task: ModelLlmEvalTask) -> ModelLlmEvalSample:
        start = time.monotonic()
        try:
            output = self._client.complete(model_key, task.prompt)
            error = ""
        except Exception as exc:  # protocol impls may raise anything
            output = ""
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.monotonic() - start) * 1000)

        score, ruff_pass, mypy_pass, hits = self._score(task, output)

        return ModelLlmEvalSample(
            model_key=model_key,
            task_id=task.task_id,
            task_type=task.task_type,
            score=round(score, 3),
            latency_ms=latency_ms,
            output_chars=len(output),
            ruff_pass=ruff_pass,
            mypy_pass=mypy_pass,
            substring_hits=hits,
            error=error,
        )

    def _score(
        self, task: ModelLlmEvalTask, output: str
    ) -> tuple[float, bool, bool, int]:
        """Dispatch to the appropriate scorer for the task type."""
        if task.task_type == EnumLlmEvalTaskType.CODE_GENERATION:
            stripped = _strip_code_fences(output)
            score, ruff_pass, mypy_pass = _score_code_output(stripped, task.language)
            return score, ruff_pass, mypy_pass, 0

        if task.task_type == EnumLlmEvalTaskType.TEST_GENERATION:
            stripped = _strip_code_fences(output)
            score, ruff_pass, mypy_pass = _score_code_output(stripped, task.language)
            return score, ruff_pass, mypy_pass, 0

        score, hits = _score_substring_output(output, task.expected_substrings)
        return score, False, False, hits


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\n?|```$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove ``` markdown fences so ruff/mypy see raw code."""
    return _FENCE_RE.sub("", text).strip() + "\n"
