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
    # FRONTIER (OMN-13938): the hard tier saturates at 1.0 across every rung
    # from the local 35B up through the paid-cloud GLM ceiling — it does not
    # separate the frontier of the ladder. Frontier tasks are engineered to
    # defeat pattern-matching rather than merely raise nominal difficulty:
    # long-chain state-machine traces requiring genuine multi-step simulation,
    # an efficiency-gated code task that fails a memorized O(n^2) textbook
    # solution, and a long-context faithfulness task under an adversarial
    # correction. See escalating_corpus.yaml's FRONTIER section comment for
    # the calibration methodology and a deferred (not-shipped) novel-puzzle
    # design that proved operationally unstable on the reachable ladder.
    FRONTIER = "frontier"


# Difficulty weights used for the weighted per-rung score. Harder tasks
# discriminate rung capability more, so they carry more weight. FRONTIER is
# weighted above HARD so a single frontier miss meaningfully moves the
# weighted score (OMN-13938).
TIER_WEIGHT: dict[EnumBenchmarkTier, int] = {
    EnumBenchmarkTier.EASY: 1,
    EnumBenchmarkTier.MEDIUM: 2,
    EnumBenchmarkTier.HARD: 3,
    EnumBenchmarkTier.FRONTIER: 4,
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
    """One rung of the delegation ladder under test (local GPU or paid cloud).

    Rungs are ordered floor -> ceiling by ``order``. Local site-specific endpoints
    are resolved from ``endpoint_url_env`` (capability-named, per the bifrost
    overlay convention) at record time only — the committed rung config never
    embeds a site-specific host/IP. Public CLOUD endpoints carry the complete URL
    directly in ``endpoint_url`` (public URLs are safe to commit) and authenticate
    with the Bearer key named by ``api_key_env``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str = Field(description="Stable rung identifier, e.g. rung_4090_reasoner.")
    order: int = Field(description="Floor->ceiling ordinal; 0 is the floor rung.")
    model_name: str = Field(description="Model id served on this rung.")
    backend_id: str = Field(description="Canonical routing_tiers backend id.")
    endpoint_url_env: str = Field(
        default="",
        description=(
            "Env var holding the COMPLETE chat-completions URL for a local "
            "site-specific endpoint. Empty for public cloud rungs."
        ),
    )
    endpoint_url: str = Field(
        default="",
        description=(
            "COMPLETE public chat-completions URL for cloud rungs, transcribed "
            "from bifrost_delegation.yaml (public URLs only — never a host/IP)."
        ),
    )
    api_key_env: str = Field(
        default="",
        description=(
            "Env var NAME (not value) holding the Bearer API key for cloud rungs; "
            "sent as Authorization: Bearer <key> at record time. Empty for local."
        ),
    )
    extra_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra HTTP headers for the rung (e.g. OpenRouter attribution).",
    )
    gpu: str = Field(description="Accelerator label, e.g. rtx_4090 / rtx_5090 / cloud.")
    host_label: str = Field(
        description="Human label of the serving host (AI-PC / Mac-Studio / cloud), no IP."
    )
    tier_name: str = Field(
        default="local",
        description="routing_tiers.yaml tier (local / cheap_cloud / cheap_frontier).",
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
    # OMN-13938: True when the recorded cell is an infra-availability block this
    # session (endpoint unreachable / quota exhausted — never actually attempted
    # the task), as opposed to a genuine capability failure (endpoint reached,
    # request sent, model timed out or answered wrong). Blocked cells are
    # excluded from rung scoring (neither counted for nor against) so a
    # same-week rate limit cannot manufacture a false capability regression;
    # they are NOT dropped from the packet — the block is recorded honestly.
    blocked: bool = False


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
