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
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)


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


class EnumContextRoiRowKind(StrEnum):
    """Kind of row a ModelAttemptReductionRow represents (OMN-23628).

    try      -- one code-eval try with its check verdict and timings.
    control  -- a control row kept beside the tries for comparison.
    """

    TRY = "try"
    CONTROL = "control"


class ModelAttemptReductionRow(BaseModel):
    """Per-run row captured by the context-ROI runner.

    One row per (task x factor_subset x trial).  Captured live
    (RUNTIME_OBSERVED) from typed event fields -- never from log scraping.
    Freeze rows as fixtures so the scorer is REPLAY_PROVEN (mirrors
    OMN-12661).

    All non-identity fields are optional with safe defaults so the scorer
    can deserialise rows from fixtures that predate field additions.

    The code-eval trial fields (OMN-23628) describe one code-eval try:
    check_verdict / unscorable_source record the contract-score outcome;
    row_kind distinguishes a live try from a control row; try_end records
    how the try ended (the loop's status or a unit timeout); suite_digest
    and size_band identify the evaluated suite and its size class;
    sequence_id / try_ordinal / diff_hash / max_turns / try_timeout_s /
    generation_s / env_sync_s / check_s carry the trial's identity, ordinal,
    diff, and per-phase timing. A row written before these fields were
    added reads their defaults (e.g. check_verdict=None, row_kind=TRY,
    size_band=S, all timings 0.0), so older fixtures still validate.
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
    factor_subset_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the canonical (ordered) factor-subset label "
            "set for this arm; empty string for the off arm (no factors). "
            "Replay-audits which factor combination produced the row so a "
            "'winning factor' is attributable (BAC plan line 111)."
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
    routing_source: str = Field(
        default="",
        description=(
            "Provenance of the model/endpoint selection for this row, resolved "
            "from the routing authority (e.g. 'routing_tier:local-coder'); empty "
            "string when the routing source was not captured. Completes the "
            "per-row metadata set so a winning factor is not a routing artifact "
            "(BAC plan line 111)."
        ),
    )

    # Code-eval trial fields (OMN-23628) -- describe one code-eval try
    check_verdict: EnumCodeScoreVerdict | None = Field(
        default=None,
        description=(
            "Contract-score verdict for this try; None when the try was not "
            "scored. When unscorable_source is set, this must be UNSCORABLE."
        ),
    )
    unscorable_source: EnumCodeScoreSource | None = Field(
        default=None,
        description=(
            "Why this try could not be scored (an infrastructure fault, never "
            "the draft's own); None when the try was scored. Allowed only when "
            "check_verdict is UNSCORABLE."
        ),
    )
    row_kind: EnumContextRoiRowKind = Field(
        default=EnumContextRoiRowKind.TRY,
        description=(
            "Kind of row: 'try' for a live code-eval trial, 'control' for a "
            "baseline comparison row. Defaults to TRY so older rows read as tries."
        ),
    )
    try_end: str = Field(
        default="",
        description=(
            "How the try ended: the loop's terminal status or a unit timeout "
            "label; empty string when not captured."
        ),
    )
    suite_digest: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the evaluated suite; empty string when the "
            "suite was not identified (e.g. off arm or unscorable)."
        ),
    )
    size_band: EnumSizeBand = Field(
        default=EnumSizeBand.S,
        description=(
            "Size band of the task this try ran (S, M or L); defaults to S, the "
            "band every row was filed under before this field."
        ),
    )
    sequence_id: str = Field(
        default="",
        description=(
            "Identifier of the sequence of tries this try belongs to; empty "
            "string when not captured."
        ),
    )
    try_ordinal: int = Field(
        default=0,
        ge=0,
        description="Ordinal of this try within its sequence (0 = unset).",
    )
    diff_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the diff produced by this try; empty string "
            "when no diff was captured."
        ),
    )
    max_turns: int = Field(
        default=0,
        ge=0,
        description="Maximum turns allowed for this try (0 = not set).",
    )
    try_timeout_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Timeout in seconds applied to this try (0.0 = not set).",
    )
    generation_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds spent in the generation phase (0.0 = not captured).",
    )
    env_sync_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds spent in the environment-sync phase (0.0 = not captured).",
    )
    check_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds spent in the check phase (0.0 = not captured).",
    )

    # Evidence classification
    proof_class: EnumProofClass = Field(
        default=EnumProofClass.RUNTIME_OBSERVED_ONLY,
        description=(
            "REPLAY_PROVEN when row was re-scored from frozen fixtures; "
            "RUNTIME_OBSERVED_ONLY when captured from a live run."
        ),
    )

    @model_validator(mode="after")
    def _unscorable_source_needs_unscorable_verdict(self) -> Self:
        if (
            self.unscorable_source is not None
            and self.check_verdict is not EnumCodeScoreVerdict.UNSCORABLE
        ):
            raise ValueError(
                "unscorable_source must be None unless check_verdict is UNSCORABLE"
            )
        return self


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
    excluded_unpriced_trials: int = Field(
        default=0,
        ge=0,
        description=(
            "Trials left out of rows because the run's model is unpriced "
            "(null cost_inference_usd, OMN-20834): excluded, never counted at 0.0"
        ),
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Non-fatal warnings from the run (e.g. optional factor absent)",
    )


__all__ = [
    "EnumContextRoiRowKind",
    "EnumFailureStage",
    "ModelAttemptReductionRow",
    "ModelContextRoiRunResult",
]
