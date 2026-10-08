# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for node_typed_decision_effect (OMN-19432).

A typed decision is one question over a program state, answered by a choice,
a yes/no probability (a "noul") or a position on ordered levels (a score). The
request optionally carries the INCUMBENT's answer: the deterministic answer
the caller already has. When present, the incumbent answers whenever the model
does not: on a refusal, on a backend error, and below the contract's abstention
threshold. Without an incumbent, the request is blind and those outcomes
produce no answer.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

#: ``owner/name``, the shape GitHub accepts for a repository slug.
_REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class EnumTypedDecisionKind(StrEnum):
    """The three typed-question primitives."""

    CHOICE = "choice"
    NOUL = "noul"
    SCORE = "score"


class EnumTypedDecisionDecider(StrEnum):
    """Which decider produced ``answer``. A fallback is never labelled the model's."""

    MODEL = "model"
    NO_ANSWER = "no_answer"
    INCUMBENT_ABSTAINED = "incumbent_abstained"
    INCUMBENT_REFUSED = "incumbent_refused"
    INCUMBENT_BACKEND_ERROR = "incumbent_backend_error"
    INCUMBENT_SHADOW = "incumbent_shadow"


class EnumTypedDecisionReason(StrEnum):
    """Why the model did not decide, with an optional incumbent fallback."""

    # Refusals: decided before any call to the decision backend.
    NO_REPOSITORY_ATTRIBUTION = "no_repository_attribution"
    REPOSITORY_NOT_PUBLIC = "repository_not_public"
    REPOSITORY_VISIBILITY_UNRESOLVED = "repository_visibility_unresolved"
    BACKEND_UNRESOLVED = "backend_unresolved"
    CREDENTIAL_NOT_REGISTERED = "credential_not_registered"
    # Backend errors: the call was made and produced no usable answer.
    BACKEND_HTTP_ERROR = "backend_http_error"
    BACKEND_TRANSPORT_ERROR = "backend_transport_error"
    BACKEND_MALFORMED_RESPONSE = "backend_malformed_response"
    # Abstention: a schema-valid answer below the contract threshold.
    BELOW_ABSTENTION_THRESHOLD = "below_abstention_threshold"


class ModelTypedDecisionRequest(BaseModel):
    """One typed question with an optional incumbent; absent means blind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    work_unit_repository: str | None = Field(
        default=None,
        description=(
            "The ``owner/name`` repository the WORK UNIT belongs to, never the "
            "repository the caller lives in. Its visibility is resolved live "
            "at request time; a missing, private or unresolvable repository "
            "is refused before any call to the decision backend."
        ),
    )
    state: JsonValue = Field(
        ..., description="The program state the question is asked over."
    )
    kind: EnumTypedDecisionKind
    instructions: str | dict[str, JsonValue] | list[JsonValue] = Field(
        ..., description="The question itself, in full: the model sees no id."
    )
    criteria: JsonValue = Field(
        default=None,
        description=(
            "choice: a mapping of option to its description (or null). "
            "score: an ordered list of at least two level descriptions. "
            "noul: optional mapping with 'true' and/or 'false' descriptions."
        ),
    )
    incumbent_answer: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The optional deterministic answer the caller already has, in the same "
            "vocabulary as a model answer: a choice option, 'true'/'false' "
            "for a noul, or a level index for a score. When absent, the request is blind."
        ),
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> ModelTypedDecisionRequest:
        if self.work_unit_repository is not None and not _REPOSITORY_SLUG.match(
            self.work_unit_repository
        ):
            raise ValueError(
                "work_unit_repository must be an owner/name slug, got "
                f"{self.work_unit_repository!r}"
            )
        if self.kind is EnumTypedDecisionKind.CHOICE:
            if not isinstance(self.criteria, dict) or len(self.criteria) < 2:
                raise ValueError("a choice needs criteria mapping at least two options")
            if (
                self.incumbent_answer is not None
                and self.incumbent_answer not in self.criteria
            ):
                raise ValueError("incumbent_answer must be one of the choice options")
        elif self.kind is EnumTypedDecisionKind.SCORE:
            if not isinstance(self.criteria, list) or not 2 <= len(self.criteria) <= 10:
                raise ValueError("a score needs criteria listing 2 to 10 levels")
            if self.incumbent_answer is not None and self.incumbent_answer not in {
                str(i) for i in range(len(self.criteria))
            }:
                raise ValueError("incumbent_answer must be a level index of the score")
        else:
            if self.criteria is not None and (
                not isinstance(self.criteria, dict)
                or not set(self.criteria) <= {"true", "false"}
            ):
                raise ValueError("noul criteria may only describe 'true' and 'false'")
            if self.incumbent_answer is not None and self.incumbent_answer not in {
                "true",
                "false",
            }:
                raise ValueError("a noul incumbent_answer is 'true' or 'false'")
        return self


class ModelTypedDecisionResult(BaseModel):
    """The answer to use (None for NO_ANSWER), its decider, and receipt fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    decided_by: EnumTypedDecisionDecider
    answer: str | None = Field(
        ..., description="The answer the caller acts on; None exactly for NO_ANSWER."
    )
    reason: EnumTypedDecisionReason | None = Field(
        default=None, description="Why the model did not decide. None for MODEL."
    )
    model_answer: str | None = Field(
        default=None,
        description="The model's own answer when one was returned, even if unused.",
    )
    probability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Probability of ``model_answer``; compared with the threshold.",
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)
    score: float | None = Field(
        default=None, description="A score's probability-weighted level position."
    )
    abstain_below_probability: float = Field(..., ge=0.0, le=1.0)
    backend_id: str
    requested_model: str | None = None
    served_model: str | None = Field(
        default=None, description="The concrete model the provider says answered."
    )
    http_status: int | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    detail: str | None = Field(
        default=None,
        description="A non-secret line explaining a refusal or an error.",
    )

    @model_validator(mode="after")
    def _validate_answer(self) -> ModelTypedDecisionResult:
        if (self.answer is None) != (
            self.decided_by is EnumTypedDecisionDecider.NO_ANSWER
        ):
            raise ValueError("answer must be None exactly when decided_by is NO_ANSWER")
        return self


class ModelTypedDecisionShadowResult(ModelTypedDecisionResult):
    """A new-topic receipt; the direct effect's existing wire shape stays intact."""

    decided_by: Literal[EnumTypedDecisionDecider.INCUMBENT_SHADOW] = (
        EnumTypedDecisionDecider.INCUMBENT_SHADOW
    )
    shadow_decided_by: EnumTypedDecisionDecider = Field(
        ...,
        description="The backend outcome observed without steering a shadow decision.",
    )

    @model_validator(mode="after")
    def _validate_shadow_outcome(self) -> ModelTypedDecisionShadowResult:
        if self.shadow_decided_by is EnumTypedDecisionDecider.INCUMBENT_SHADOW:
            raise ValueError("shadow_decided_by must name the observed backend outcome")
        return self


class ModelTypedDecisionCalibrationObservation(BaseModel):
    """One independently adjudicated decision, never an incumbent-derived label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    probabilities: dict[str, float]
    adjudicated_answer: str

    @model_validator(mode="after")
    def _validate_distribution(self) -> ModelTypedDecisionCalibrationObservation:
        if len(self.probabilities) < 2 or any(
            not math.isfinite(value) or not 0.0 <= value <= 1.0
            for value in self.probabilities.values()
        ):
            raise ValueError(
                "probabilities must be a finite distribution over two or more options"
            )
        if not math.isclose(sum(self.probabilities.values()), 1.0, abs_tol=1e-6):
            raise ValueError("probabilities must sum to one")
        if self.adjudicated_answer not in self.probabilities:
            raise ValueError("adjudicated_answer must be an offered option")
        return self


class ModelTypedDecisionCalibrationRequest(BaseModel):
    """A fixed cohort of distinct, adjudicated decisions for one option vocabulary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelTypedDecisionCalibrationObservation, ...]

    @model_validator(mode="after")
    def _validate_cohort(self) -> ModelTypedDecisionCalibrationRequest:
        if len({row.correlation_id for row in self.observations}) != len(
            self.observations
        ):
            raise ValueError(
                "calibration decisions must have unique correlation identifiers"
            )
        if self.observations:
            options = set(self.observations[0].probabilities)
            if any(set(row.probabilities) != options for row in self.observations):
                raise ValueError(
                    "all calibration decisions must offer the same options"
                )
        return self


class ModelTypedDecisionReliabilityBin(BaseModel):
    """One reliability diagram cell; empty cells carry no inferred measurements."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lower: float
    upper: float
    count: int
    mean_probability: float | None
    observed_frequency: float | None


class ModelTypedDecisionCalibrationReport(BaseModel):
    """Below the contract's sample floor, every calibration metric is absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_count: int = Field(..., ge=0)
    minimum_samples: int = Field(..., ge=300)
    reason: Literal["insufficient_samples"] | None = None
    brier_score: float | None = Field(default=None, ge=0.0, le=2.0)
    per_option_ece: dict[str, float] | None = None
    reliability_diagram: (
        dict[str, tuple[ModelTypedDecisionReliabilityBin, ...]] | None
    ) = None

    @model_validator(mode="after")
    def _validate_sample_floor(self) -> ModelTypedDecisionCalibrationReport:
        metrics = (self.brier_score, self.per_option_ece, self.reliability_diagram)
        if self.sample_count < self.minimum_samples:
            if (
                any(value is not None for value in metrics)
                or self.reason != "insufficient_samples"
            ):
                raise ValueError(
                    "calibration metrics are refused below the sample floor"
                )
        elif any(value is None for value in metrics) or self.reason is not None:
            raise ValueError("a sufficient cohort must carry every calibration metric")
        return self


class ModelTypedDecisionWorkflowRequest(BaseModel):
    """Bus-addressable shadow decision or calibration report; no live-arm mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["typed_decision_shadow"] = "typed_decision_shadow"
    decision: ModelTypedDecisionRequest | None = None
    calibration: ModelTypedDecisionCalibrationRequest | None = None

    @model_validator(mode="after")
    def _validate_operation(self) -> ModelTypedDecisionWorkflowRequest:
        if (self.decision is None) == (self.calibration is None):
            raise ValueError("provide exactly one of decision or calibration")
        if self.decision is not None and self.decision.incumbent_answer is None:
            raise ValueError("a shadow decision requires the incumbent answer")
        return self


class ModelTypedDecisionWorkflowResult(BaseModel):
    """The terminal event's shadow receipt or sample-gated calibration report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ModelTypedDecisionShadowResult | None = None
    calibration: ModelTypedDecisionCalibrationReport | None = None


__all__: list[str] = [
    "EnumTypedDecisionDecider",
    "EnumTypedDecisionKind",
    "EnumTypedDecisionReason",
    "ModelTypedDecisionCalibrationObservation",
    "ModelTypedDecisionCalibrationReport",
    "ModelTypedDecisionCalibrationRequest",
    "ModelTypedDecisionReliabilityBin",
    "ModelTypedDecisionRequest",
    "ModelTypedDecisionResult",
    "ModelTypedDecisionShadowResult",
    "ModelTypedDecisionWorkflowRequest",
    "ModelTypedDecisionWorkflowResult",
]
