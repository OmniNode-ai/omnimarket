# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed DTOs for the escalating-complexity graded ladder benchmark (OMN-13935)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBenchmarkTier(StrEnum):
    """Difficulty tier of a benchmark task — the escalating-complexity axis."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


# Difficulty weights used for the weighted per-rung score. Harder tasks
# discriminate rung capability more, so they carry more weight.
TIER_WEIGHT: dict[EnumBenchmarkTier, int] = {
    EnumBenchmarkTier.EASY: 1,
    EnumBenchmarkTier.MEDIUM: 2,
    EnumBenchmarkTier.HARD: 3,
}


class EnumGraderKind(StrEnum):
    """Objective, deterministic grader kinds.

    Every grader returns a hard pass/fail from the recorded rung output — no
    LLM-judge, no heuristic marker counting. This is what makes rung separation
    a genuine capability signal rather than a scoring artifact.
    """

    NUMERIC = "numeric"  # final integer/number in the answer equals expected
    CONTAINS = "contains"  # answer contains the expected substring
    CODE_EXEC = "code_exec"  # extracted python passes a fixed assertion harness


class ModelLadderRung(BaseModel):
    """One rung of the local delegation ladder under test.

    Rungs are ordered floor -> ceiling by ``order``. Endpoints are resolved from
    ``endpoint_url_env`` (capability-named, per the bifrost overlay convention) at
    record time only — the committed rung config never embeds a site-specific
    host/IP.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str = Field(description="Stable rung identifier, e.g. rung_4090_reasoner.")
    order: int = Field(description="Floor->ceiling ordinal; 0 is the floor rung.")
    model_name: str = Field(description="Model id served on this rung.")
    backend_id: str = Field(description="Canonical routing_tiers backend id.")
    endpoint_url_env: str = Field(
        description="Env var holding the COMPLETE chat-completions URL for capture."
    )
    gpu: str = Field(description="Accelerator label, e.g. rtx_4090 / rtx_5090.")
    host_label: str = Field(
        description="Human label of the serving host (AI-PC / Mac-Studio), no IP."
    )
    tier_name: str = Field(
        default="local",
        description="routing_tiers.yaml tier this rung belongs to (all local here).",
    )


class ModelLadderTask(BaseModel):
    """One escalating-complexity benchmark task with an objective grader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    benchmark_tier: EnumBenchmarkTier
    task_class: str = Field(description="Delegation task-class the task exercises.")
    prompt: str
    grader: EnumGraderKind
    # Grader expectations (only the field(s) relevant to `grader` are used):
    expected_number: float | None = None
    expected_substring: str | None = None
    case_sensitive: bool = True
    # For CODE_EXEC: python assertion body run against the extracted candidate.
    code_asserts: str | None = None
    entrypoint: str | None = Field(
        default=None, description="Required def name for CODE_EXEC tasks."
    )


class ModelGradedCell(BaseModel):
    """Result of grading one (rung, task) cell against a recorded output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str
    task_id: str
    benchmark_tier: EnumBenchmarkTier
    grader: EnumGraderKind
    passed: bool
    detail: str = ""
    output_recorded: bool = True
    output_chars: int = 0
    latency_ms: int = 0
    model_name: str = ""


class ModelRungScore(BaseModel):
    """Rolled-up graded score for a single rung across the whole corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str
    order: int
    model_name: str
    gpu: str
    tasks_total: int
    tasks_passed: int
    pass_rate: float  # unweighted fraction of tasks passed (0..1)
    weighted_score: float  # difficulty-weighted fraction passed (0..1)
    per_tier_pass_rate: dict[str, float] = Field(default_factory=dict)


class ModelSeparationVerdict(BaseModel):
    """The acceptance verdict: does the benchmark separate floor from ceiling?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_rung_id: str
    ceiling_rung_id: str
    floor_score: float
    ceiling_score: float
    margin: float  # ceiling_score - floor_score
    required_margin: float
    separated: bool
    monotonic_nondecreasing: bool = Field(
        description="True if weighted scores never decrease floor->ceiling."
    )
    reasons: tuple[str, ...] = Field(default_factory=tuple)


class ModelBenchmarkPacket(BaseModel):
    """Full evidence packet emitted by a benchmark run."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = "OMN-13935"
    gate: str = "delegation_graded_ladder_benchmark"
    rungs: list[ModelLadderRung] = Field(default_factory=list)
    n_tasks: int = 0
    tiers: list[str] = Field(default_factory=list)
    cells: list[ModelGradedCell] = Field(default_factory=list)
    rung_scores: list[ModelRungScore] = Field(default_factory=list)
    separation: ModelSeparationVerdict | None = None
    fixture_source: str = ""
    corpus_source: str = ""
    rungs_source: str = ""
    passed: bool = False
    failures: list[str] = Field(default_factory=list)
