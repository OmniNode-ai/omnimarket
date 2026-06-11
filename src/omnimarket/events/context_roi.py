# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared context-ROI experiment event payload models (OMN-12955).

Canonical home for the context-ROI runner terminal-event payload models:
:class:`EnumFailureStage`, :class:`ModelAttemptReductionRow`, and
:class:`ModelContextRoiRunResult`. These are the typed payload carried on
``onex.evt.omnimarket.context-roi-run-completed.v1`` and consumed by more than
one node (the runner EFFECT that emits them, the COMPUTE scorer that re-scores
them, and the projection reducer that materializes them into context_roi_scores).

Promoted out of node_context_roi_runner.models into omnimarket.events so that
consuming nodes read a shared event model instead of reaching into another
node's private models package (enforced by tests/test_no_cross_node_reach_in.py).
EnumProofClass stays in omnimarket.enums.enum_proof_class (shared).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_proof_class import EnumProofClass


class EnumFailureStage(StrEnum):
    """Where in the generation pipeline the first failure occurred.

    none            -- no failure; generation succeeded within max_attempts.
    pack_build      -- context pack assembly failed (budget exceeded, missing
                      required factor, unrecognised factor labels, etc.)
                      before any LLM call.
    budget_fail     -- pack exceeded the token budget hard-reject; a sub-class
                      of pack_build, surfaced separately so scorers can
                      distinguish budget rejections from other pack errors.
    generation      -- LLM call failed, timed out, or no terminal event
                      arrived within generation_timeout_seconds.
    validation      -- LLM produced output but contract validation failed on
                      every attempt (schema / syntax / security checks).
    downstream_gate -- generation+validation succeeded but a downstream quality
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
    (RUNTIME_OBSERVED) from typed event fields -- never from log scraping.
    Freeze rows as fixtures so the scorer is REPLAY_PROVEN (mirrors
    OMN-12661).

    All non-identity fields are optional with safe defaults so the scorer
    can deserialise rows from fixtures that predate field additions.
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
    run_order: int = Field(
        default=0,
        ge=0,
        description=(
            "Global execution order of this (task x arm x trial) cell within "
            "the run (1-based; 0 = unset). Recorded so downstream analysis can "
            "control for intra-run order effects."
        ),
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

    # Generation telemetry -- sourced from typed event fields (never log scraping)
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

    # Token accounting -- prompt/completion split from typed event fields
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

    # Model / routing identity -- from contract/overlay, never hardcoded
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


class ModelContextRoiRunResult(BaseModel):
    """Terminal output for one full experiment run.

    rows holds one entry per (task x arm x trial).  Freeze these rows as
    fixtures so the scorer node is REPLAY_PROVEN (mirrors OMN-12661).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Echoed from the request run_id")
    rows: tuple[ModelAttemptReductionRow, ...] = Field(
        description="Per-(task x arm x trial) attempt-reduction rows"
    )
    proof_class: EnumProofClass = Field(
        default=EnumProofClass.RUNTIME_OBSERVED_ONLY,
        description=(
            "REPLAY_PROVEN when rows were re-scored from frozen fixtures; "
            "RUNTIME_OBSERVED_ONLY when captured from a live run."
        ),
    )
    total_trials: int = Field(
        default=0,
        ge=0,
        description="Total number of (task x arm x trial) cells attempted",
    )
    failed_trials: int = Field(
        default=0,
        ge=0,
        description="Number of trials that ended in a non-none failure_stage",
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Non-fatal warnings from the run (e.g. optional factor absent)",
    )


__all__ = [
    "EnumFailureStage",
    "ModelAttemptReductionRow",
    "ModelContextRoiRunResult",
]
