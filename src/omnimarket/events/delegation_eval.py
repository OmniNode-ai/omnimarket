# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared delegation-evaluation label and run event models."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from omnimarket.models.delegation_gate_eval.model_generation_stability_request import (
    ModelGenerationStabilityRequest,
)


class ModelLabelRecordRequest(BaseModel):
    """One label of one delegation attempt, without a transport envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    correlation_id: str = Field(min_length=1)
    attempt_index: int = Field(ge=0)
    label: str = Field(min_length=1)
    rater_role: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    stratum: str
    computed_facts: dict[str, JsonValue]


class ModelDelegationEvalItemLabelled(ModelLabelRecordRequest):
    """Scrubbed label event persisted only in the lab table."""

    prompt_snapshot: str
    response_snapshot: str
    task_class: str
    gate_verdict: str
    deciding_check: str
    observed_at: AwareDatetime


class ModelDelegationEvalRunRequest(BaseModel):
    """One eval run over a manifest's labelled items (EV.4, OMN-19793).

    The labels are selected by rater role and rubric version, so a run never
    mixes raters. ``gate_version`` names the installed gate the replayed arm
    exercises; it is part of the eval run id, so a gate change is a new run.
    ``generation_stability`` (OMN-19795, EV.7) carries the three gated verdicts
    of each repeated generation on one backend; when present, each class's
    results rows carry its flip rate, the Wilson interval and that backend, and
    the groups' digest is part of the eval run id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    manifest_id: str = Field(min_length=1)
    rater_role: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    gate_version: str = Field(min_length=1)
    harness: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    model: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    configuration: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    generation_stability: ModelGenerationStabilityRequest | None = None


class ModelDelegationEvalItemVerdict(BaseModel):
    """One labelled item's recorded and replayed verdicts. Content-free."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_key: str = Field(min_length=1)
    task_class: str
    stratum: str
    label: str
    recorded_verdict: str | None
    recorded_deciding_check: str | None
    replayed_verdict: str
    replayed_deciding_check: str | None
    replay_count: int = Field(ge=1)
    duration_s: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, exclude_if=lambda value: value is None
    )
    failure_kind: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ModelDelegationEvalResultRow(BaseModel):
    """Per class, stratum and arm: false passes first, then false refusals.

    A rate is ``None`` when its population is empty. ``*_upper_bound`` is the
    decision bound of ``omnimarket.ranges`` restated as an error rate (one minus
    the pass-rate lower bound); the Wilson interval is printed beside it.
    ``*_line_verdict`` is the range evaluator's MET, MISSED or REFUSED.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str
    stratum: str
    arm: str
    total_n: int = Field(ge=0)
    accepted_n: int = Field(ge=0)
    false_pass_count: int = Field(ge=0)
    false_pass_rate: float | None
    false_pass_upper_bound: float | None
    false_pass_wilson_low: float
    false_pass_wilson_high: float
    false_pass_line_verdict: str
    false_pass_required_n: int = Field(ge=0)
    refused_n: int = Field(ge=0)
    false_refusal_count: int = Field(ge=0)
    false_refusal_rate: float | None
    false_refusal_upper_bound: float | None
    false_refusal_wilson_low: float
    false_refusal_wilson_high: float
    false_refusal_line_verdict: str
    undetermined_n: int = Field(ge=0)
    undetermined_share: float
    # OMN-19795: the class's generation flip rate, measured on one backend; None
    # when the run carried no repeated generations for the class.
    flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    flip_rate_wilson_low: float | None = Field(default=None, ge=0.0, le=1.0)
    flip_rate_wilson_high: float | None = Field(default=None, ge=0.0, le=1.0)
    flip_measured_backend_id: str | None = Field(default=None, min_length=1)


class ModelDelegationEvalRunCompleted(BaseModel):
    """The run's one terminal event. Content-free: keys, labels and counts only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    eval_run_id: UUID
    manifest_id: str = Field(min_length=1)
    label_set_sha256: str
    gate_version: str
    rater_role: str
    rubric_version: str
    status: str
    failure_reasons: tuple[str, ...] = ()
    item_verdicts: tuple[ModelDelegationEvalItemVerdict, ...] = ()
    results: tuple[ModelDelegationEvalResultRow, ...] = ()
    observed_at: AwareDatetime
    harness: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    model: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    configuration: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
