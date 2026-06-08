# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Attempt-reduction result models for OMN-12794 (P2-1 SEAM).

These models are captured by the context-ROI runner (an EFFECT node) and
scored offline by the context-ROI compute node (a pure COMPUTE node).  They
extend the generation pipeline with per-run context and attempt telemetry.

Placement: experiment-private to omnimarket; not promoted to omnibase_core
unless a second repo imports them (per A5 layer rule).

EnumProofClass is imported from omnimarket.enums.enum_proof_class (promoted
there in OMN-12794 P2-1 because two nodes now reference it).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_proof_class import EnumProofClass


class EnumFailureStage(StrEnum):
    """Where in the generation pipeline the first failure occurred.

    none            — no failure; generation succeeded within max_attempts.
    pack_build      — context pack assembly failed (budget exceeded, missing
                      required factor, etc.) before any LLM call.
    budget_fail     — pack exceeded the token budget hard-reject; a sub-class
                      of pack_build, surfaced separately so scorers can
                      distinguish budget rejections from other pack errors.
    generation      — all LLM attempts exhausted without a successful raw output.
    validation      — LLM produced output but contract validation failed on every
                      attempt (schema / syntax / security checks).
    downstream_gate — generation+validation succeeded but a downstream quality
                      gate (pytest / is_healthy probe) failed.
    """

    NONE = "none"
    PACK_BUILD = "pack_build"
    BUDGET_FAIL = "budget_fail"
    GENERATION = "generation"
    VALIDATION = "validation"
    DOWNSTREAM_GATE = "downstream_gate"


class ModelAttemptReductionRow(BaseModel):
    """Per-run row captured by the context-ROI runner.

    One row per (task x factor_subset x trial).  Captured live
    (RUNTIME_OBSERVED) from typed event fields — never from log scraping.
    Frozen as a fixture so the scorer is REPLAY_PROVEN.

    All new fields are optional with safe defaults so existing consumers
    that predate this model extension continue to deserialise cleanly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Correlation / identity
    run_id: str = Field(description="Stable run identifier for this experiment run")
    correlation_id: str = Field(
        description="Per-(task x subset x trial) correlation ID minted by the runner"
    )
    task_id: str = Field(
        description="Stable task identifier from the fixed task manifest"
    )

    # Factor subset that was injected for this trial
    context_factor_subset: str = Field(
        default="off",
        description=(
            "Label for the factor subset used, e.g. 'off', 'golden_only', "
            "'golden_exemplar', 'structured_context'. Matches the N-arm matrix."
        ),
    )
    context_pack_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the serialised context pack content; "
            "empty string when no pack was injected (off arm). "
            "Enables exact replay of the injected context."
        ),
    )

    # Generation telemetry — sourced from typed event fields (never log scraping)
    attempt_count: int = Field(
        default=0, ge=0, description="Total generation attempts made (0 = not started)"
    )
    first_pass_success: bool = Field(
        default=False,
        description=(
            "True when contract validation passed on attempt 1 "
            "(attempts[0].contract_passed). "
            "Not the same as final_success."
        ),
    )
    final_success: bool = Field(
        default=False,
        description=(
            "True when contract validation passed within max_attempts "
            "(contract_passed on the terminal attempt). "
            "Distinct from quality_gate_passed / invocation_healthy."
        ),
    )
    failure_stage: EnumFailureStage = Field(
        default=EnumFailureStage.NONE,
        description=(
            "Stage at which the first unrecoverable failure occurred. "
            "'none' when final_success is True."
        ),
    )

    # Token accounting — prompt/completion split from typed event fields
    prompt_tokens: int = Field(
        default=0, ge=0, description="Total prompt tokens across all attempts"
    )
    completion_tokens: int = Field(
        default=0, ge=0, description="Total completion tokens across all attempts"
    )
    estimated_cost: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated inference cost in USD across all attempts",
    )

    # Model / routing identity — from contract/overlay, never hardcoded
    model_id: str = Field(default="", description="Model ID used for generation")
    provider: str = Field(default="", description="LLM provider (local, gemini, etc.)")
    endpoint_ref: str = Field(
        default="", description="Routing-tier endpoint reference (e.g. 'local-coder')"
    )

    # Evidence classification
    proof_class: EnumProofClass = Field(
        default=EnumProofClass.RUNTIME_OBSERVED_ONLY,
        description=(
            "REPLAY_PROVEN when row was re-scored from frozen fixtures; "
            "RUNTIME_OBSERVED_ONLY when captured from a live run."
        ),
    )


__all__ = [
    "EnumFailureStage",
    "ModelAttemptReductionRow",
]
