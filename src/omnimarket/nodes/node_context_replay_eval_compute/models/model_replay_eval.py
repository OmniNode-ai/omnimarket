# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed replay evaluation inputs and text-free scoring outputs (OMN-21111)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelExpectedQuote(_Frozen):
    statement_id: str
    quote: str | None = None
    required_spans: tuple[str, ...] = ()
    withheld_reason: str | None = None


class ModelPresenceCase(_Frozen):
    case_id: str
    candidate_id: str | None = None
    package_text: str
    expected: tuple[ModelExpectedQuote, ...] = Field(min_length=1)


class ModelRulingPresenceRequest(_Frozen):
    cases: tuple[ModelPresenceCase, ...] = Field(min_length=1)


class EnumPresence(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    UNSCORABLE = "UNSCORABLE"


class ModelStatementPresence(_Frozen):
    statement_id: str
    presence: EnumPresence


class ModelCasePresence(_Frozen):
    case_id: str
    candidate_id: str | None
    statements: tuple[ModelStatementPresence, ...]
    passed: bool


class ModelRulingPresenceResult(_Frozen):
    cases: tuple[ModelCasePresence, ...]
    cases_total: int
    cases_passed: int
    all_present: bool
    falsifiers: tuple[str, ...]


class EnumAnswerGrade(StrEnum):
    """UNAVAILABLE means no answer could be asked, such as an uncaptured native summary."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class ModelCaseAnswer(_Frozen):
    case_id: str
    candidate_id: str
    package_answer: EnumAnswerGrade
    native_answer: EnumAnswerGrade


class ModelEvaluationRun(_Frozen):
    run_id: str
    cases: tuple[ModelCaseAnswer, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> Self:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case_id within a run")
        return self


class ModelAnswerComparisonRequest(_Frozen):
    runs: tuple[ModelEvaluationRun, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_run_ids(self) -> Self:
        ids = [run.run_id for run in self.runs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate run_id")
        return self


class ModelRunAnswerCounts(_Frozen):
    run_id: str
    cases: int
    package_matches: int
    native_matches: int
    native_unavailable: int
    package_unavailable: int
    package_at_least_native: bool


class ModelAnswerComparisonResult(_Frozen):
    runs: tuple[ModelRunAnswerCounts, ...]
    next_action_met: bool
    determinism_checked: bool
    candidate_id_mismatches: tuple[str, ...]
    candidate_ids_identical: bool
    falsifiers: tuple[str, ...]


class ModelCarriedPrompt(_Frozen):
    prompt_id: str
    captured_at: AwareDatetime
    token_estimate: int = Field(ge=0)


class ModelBoundaryCarry(_Frozen):
    session_id: str
    boundary_id: str
    boundary_at: AwareDatetime
    control: bool = False
    budget_override_tokens: int | None = Field(default=None, gt=0)
    other_required_tokens: dict[str, Annotated[int, Field(ge=0)]]
    operator_words_overhead_tokens: int = Field(default=0, ge=0)
    carried_prompts: tuple[ModelCarriedPrompt, ...] = ()

    @model_validator(mode="after")
    def validate_carry(self) -> Self:
        if "OPERATOR_WORDS_UNRECORDED" in self.other_required_tokens:
            raise ValueError(
                "OPERATOR_WORDS_UNRECORDED would double count carried prompts"
            )
        if self.budget_override_tokens is not None and not self.control:
            raise ValueError("budget_override_tokens requires a control boundary")
        ids = [prompt.prompt_id for prompt in self.carried_prompts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate prompt_id within a boundary")
        return self


class ModelReviewRowSizingRequest(_Frozen):
    budget_tokens: int = Field(gt=0)
    boundaries: tuple[ModelBoundaryCarry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_boundaries(self) -> Self:
        ids = [
            (boundary.session_id, boundary.boundary_id) for boundary in self.boundaries
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate (session_id, boundary_id)")
        return self


class EnumSizingVerdict(StrEnum):
    FITS_AS_IS = "FITS_AS_IS"
    FITS_AFTER_REVIEW = "FITS_AFTER_REVIEW"
    DOES_NOT_FIT = "DOES_NOT_FIT"


class ModelBoundarySizing(_Frozen):
    session_id: str
    boundary_id: str
    boundary_at: AwareDatetime
    control: bool
    budget_tokens: int
    operator_words_tokens: int
    required_tokens: int
    exceeds_budget: bool
    verdict: EnumSizingVerdict
    prompts_to_name: int | None
    tokens_to_name: int | None
    named_prompt_ids: tuple[str, ...]
    required_tokens_after_review: int | None
    replay_reproduces: bool


class ModelSessionSizing(_Frozen):
    session_id: str
    boundaries: int
    exceeding: int
    does_not_fit: int
    review_rows: int


class ModelReviewRowSizingResult(_Frozen):
    boundaries: tuple[ModelBoundarySizing, ...]
    sessions: tuple[ModelSessionSizing, ...]
    boundaries_total: int
    exceeding: int
    fallback_share: float
    does_not_fit: int
    does_not_fit_share: float
    control_boundaries: int
    control_exceeding: int
    zero_fallback_has_control: bool
    falsifiers: tuple[str, ...]
