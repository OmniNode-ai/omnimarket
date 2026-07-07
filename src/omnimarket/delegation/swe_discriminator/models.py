# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed DTOs for the 2x2 SWE-discriminator harness (OMN-13988)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDecomposition(StrEnum):
    """The task-boundedness axis of the 2x2 factorial."""

    MONOLITH = "monolith"  # whole task handed to one worker
    DECOMPOSED = "decomposed"  # frontier decomposer emits slices, each solved


class EnumRouting(StrEnum):
    """The model-routing axis of the 2x2 factorial."""

    FRONTIER = "frontier"  # every worker call pinned to the frontier tier
    COST_ROUTED = "cost_routed"  # worker calls use the cost-routed (local) tier


class ModelSweDiscriminatorRuntimeConfig(BaseModel):
    """Operator-provided runtime inputs for live SWE-discriminator calls.

    The rung catalog remains the routing authority. This model only carries
    values that cannot live in committed config: secret material, site-local
    endpoint overrides, and run budgets.
    """

    model_config = ConfigDict(extra="forbid")

    frontier_rung_id: str = "rung_cloud_glm"
    cost_rung_id: str = "rung_5090_coder"
    api_keys_by_env: dict[str, str] = Field(default_factory=dict, repr=False)
    endpoint_urls_by_env: dict[str, str] = Field(default_factory=dict)
    endpoint_urls_by_backend_id: dict[str, str] = Field(default_factory=dict)
    model_names_by_backend_id: dict[str, str] = Field(default_factory=dict)
    decomposer_tier: EnumRouting = EnumRouting.FRONTIER
    max_retries: int = Field(default=4, ge=1)
    max_tokens: int = Field(default=16384, ge=256)


class EnumRunOutcome(StrEnum):
    """Classified outcome of ONE (task, arm) run.

    Only PASS and FAIL_WRONG are capability signals. TRUNCATED and BLOCKED are
    plumbing/infra events EXCLUDED from capability scoring — conflating a
    token-cap truncation with a wrong answer is exactly what would make the
    L3/L4 numbers lie (the OMN-13335 hazard).
    """

    PASS = "pass"  # floor passed — real capability signal
    FAIL_WRONG = "fail_wrong"  # complete artifact, floor failed — real signal
    TRUNCATED = "truncated"  # token-cap cut the answer before code — plumbing, excluded
    BLOCKED = "blocked"  # infra (rate-limit / unreachable) — excluded
    NO_ARTIFACT = "no_artifact"  # empty output, not truncated, not blocked

    @property
    def is_capability_signal(self) -> bool:
        return self in (EnumRunOutcome.PASS, EnumRunOutcome.FAIL_WRONG)

    @property
    def excluded_from_scoring(self) -> bool:
        return self in (EnumRunOutcome.TRUNCATED, EnumRunOutcome.BLOCKED)


class EnumArm(StrEnum):
    """The four cells of the 2x2. Arm C (decomposed+frontier) is load-bearing."""

    A_MONOLITH_FRONTIER = "A_monolith_frontier"
    B_MONOLITH_COST_ROUTED = "B_monolith_cost_routed"
    C_DECOMPOSED_FRONTIER = "C_decomposed_frontier"
    D_DECOMPOSED_COST_ROUTED = "D_decomposed_cost_routed"

    @property
    def decomposition(self) -> EnumDecomposition:
        return (
            EnumDecomposition.DECOMPOSED
            if self in (EnumArm.C_DECOMPOSED_FRONTIER, EnumArm.D_DECOMPOSED_COST_ROUTED)
            else EnumDecomposition.MONOLITH
        )

    @property
    def routing(self) -> EnumRouting:
        return (
            EnumRouting.FRONTIER
            if self in (EnumArm.A_MONOLITH_FRONTIER, EnumArm.C_DECOMPOSED_FRONTIER)
            else EnumRouting.COST_ROUTED
        )


class SweTask(BaseModel):
    """One repo-grounded SWE task replayed from a held-back merged PR.

    The task is served to an arm as ``task_text`` + ``context_code`` (the
    pre-fix source). The merged diff and its tests are held back: the arm never
    sees ``held_back_asserts``. Grading assembles ``grader_preamble`` + the
    arm's produced code + ``held_back_asserts`` and execs it in isolation — the
    deterministic hard floor. ``entrypoint`` must be defined by the artifact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    level: int = Field(ge=1, le=4, description="Complexity level 1..4.")
    source_pr: str = Field(description="e.g. #1618")
    source_sha: str
    task_text: str = Field(description="NL feature/bug request — no diff revealed.")
    context_code: str = Field(description="Pre-fix source the arm starts from.")
    grader_preamble: str = Field(
        default="", description="Support code prepended before the arm's artifact."
    )
    held_back_asserts: str = Field(description="Deterministic floor — never served.")
    required_defs: list[str] = Field(
        default_factory=list, description="Names the artifact MUST define."
    )
    contamination_flag: bool = Field(
        default=True, description="Public merged PR — model may have seen it."
    )


class ModelCall(BaseModel):
    """One captured LLM call (decomposer, slice worker, or monolith worker)."""

    model_config = ConfigDict(extra="forbid")

    role: str  # "decomposer" | "worker" | "monolith"
    tier: str  # "frontier" | "cost_routed"
    model_name: str
    endpoint_label: str
    prompt_chars: int
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    http_status: int = 0
    cost_usd: float = 0.0
    finish_reason: str = Field(
        default="",
        description="OpenAI finish_reason: 'length' == the token cap truncated "
        "the completion (the plumbing-vs-capability discriminator).",
    )
    error: str = ""


class ArmRun(BaseModel):
    """The captured result of running one task under one arm (pre-grading)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    arm: EnumArm
    decomposition: EnumDecomposition
    routing: EnumRouting
    n_slices: int = 0
    slice_plan: list[str] = Field(default_factory=list)
    artifact: str = Field(default="", description="Integrated code the arm produced.")
    calls: list[ModelCall] = Field(default_factory=list)
    decomposition_tax_usd: float = Field(
        default=0.0, description="Cost of the frontier decomposer call (C/D only)."
    )
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    blocked: bool = False
    truncated: bool = Field(
        default=False,
        description="A producing call hit the token cap (finish_reason=length) "
        "and left no usable code — plumbing, not capability.",
    )
    error: str = ""


class GradedRow(BaseModel):
    """One graded row: task x arm x repeat, artifact scored blind against the floor.

    A row is 'usable' when the run yielded a CAPABILITY signal (PASS or
    FAIL_WRONG). TRUNCATED and BLOCKED rows are recorded but excluded from
    capability scoring — see EnumRunOutcome.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    level: int
    arm: EnumArm
    decomposition: EnumDecomposition
    routing: EnumRouting
    repeat: int = Field(default=0, description="0-based repeat index for pass^k.")
    outcome: EnumRunOutcome
    artifact_produced: bool
    artifact_chars: int
    floor_passed: bool
    floor_detail: str
    usable: bool = Field(
        description="run yielded a capability signal (pass/fail_wrong)"
    )
    truncated: bool = False
    total_cost_usd: float = 0.0
    decomposition_tax_usd: float = 0.0
    total_latency_ms: int = 0
    n_slices: int = 0
    blocked: bool = False
    error: str = ""


class PassKCell(BaseModel):
    """pass^k aggregate for one (task, arm) cell over k independent repeats.

    ``pass_hat_k`` (the cell passes ALL scored repeats) is the headline
    reliability number; ``pass_at_1`` (any repeat passed) is reported alongside
    so the variance profile is visible. Excluded (truncated/blocked) repeats are
    dropped from the denominator — a rate-limit or a token-cap truncation cannot
    manufacture a false reliability regression.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    level: int
    arm: EnumArm
    k: int
    scored_repeats: int = Field(description="repeats that produced a capability signal")
    passes: int = Field(description="scored repeats that passed the floor")
    excluded_repeats: int = Field(description="truncated + blocked repeats (dropped)")
    pass_hat_k: bool = Field(description="passed ALL scored repeats (k>=1)")
    pass_at_1: bool = Field(description="passed at least one scored repeat")
    outcomes: list[EnumRunOutcome] = Field(default_factory=list)
    mean_cost_usd: float = 0.0


class SmokeReport(BaseModel):
    """Terminal report of the smoke battery — the go/no-go artifact."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = "OMN-13988"
    proof_class: str = "offline over captured artifacts"
    recorded_at: str = ""
    n_tasks: int = 0
    n_arms: int = 0
    k: int = 1
    rows: list[GradedRow] = Field(default_factory=list)
    cells: list[PassKCell] = Field(default_factory=list)
    usable_rows: int = 0
    truncated_rows: int = 0
    blocked_rows: int = 0
    total_rows: int = 0
    zero_usable_rows: bool = True
    frontier_model: str = ""
    cost_routed_model: str = ""
    notes: list[str] = Field(default_factory=list)
