# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen TE.5 evaluation measurements and routing adoption decisions (OMN-21142)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumGateMetric(StrEnum):
    COVERAGE = "coverage"
    RULING_RECALL = "ruling_recall"
    RECALL_BELOW_BASELINE = "recall_below_baseline"
    NEGATIVE_CONTROL = "negative_control"
    NEXT_ACTION_BELOW_CONTROL = "next_action_below_control"
    NEXT_ACTION_BELOW_BASELINE = "next_action_below_baseline"
    DUPLICATE_WORK = "duplicate_work"


class EnumAdoptionOutcome(StrEnum):
    ADOPT = "adopt"
    NO_RUNG_PASSED = "no_rung_passed"


class ModelEvalCase(_Frozen):
    case_id: str = Field(min_length=1)
    confirmed_lost_ruling: bool
    labelled_item_ids: tuple[str, ...] = Field(min_length=1)
    negative_control_ids: tuple[str, ...] = ()
    already_done_item_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_item_ids(self) -> Self:
        if len(set(self.labelled_item_ids)) != len(self.labelled_item_ids):
            raise ValueError("labelled_item_ids must be unique")
        if set(self.labelled_item_ids) & set(self.negative_control_ids):
            raise ValueError("labelled and negative control ids must be disjoint")
        return self


class ModelCaseRun(_Frozen):
    case_id: str
    repeat: int = Field(ge=1)
    recalled_item_ids: tuple[str, ...] = Field(
        default=(),
        description="All proposition-rule credits, including negative controls.",
    )
    proposed_item_ids: tuple[str, ...] = ()
    next_action_correct: bool
    tail_summary_matches: bool | None = Field(
        default=None,
        description="Role B in-flight task and last completed action match the manifest; None is unscored.",
    )
    parse_failures: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)


class ModelRungEvaluation(_Frozen):
    rung_id: str = Field(min_length=1)
    served_model_id: str = Field(min_length=1)
    mode: Literal["whole", "chunked"]
    runs: tuple[ModelCaseRun, ...]

    @model_validator(mode="after")
    def validate_run_keys(self) -> Self:
        keys = {(run.case_id, run.repeat) for run in self.runs}
        if len(keys) != len(self.runs):
            raise ValueError("duplicate (case_id, repeat) in evaluation")
        return self


class ModelRoleAdoptionVerdictRequest(_Frozen):
    evaluation_id: str = Field(min_length=1)
    task_class: str = Field(
        min_length=1, description="The role's task class, e.g. extraction_proposer."
    )
    repeats_required: int = Field(default=3, ge=1)
    cases: tuple[ModelEvalCase, ...] = Field(min_length=1)
    baseline: ModelRungEvaluation = Field(description="Native compaction summary.")
    control_without_events: ModelRungEvaluation = Field(
        description="The deterministic package without model events."
    )
    rungs: tuple[ModelRungEvaluation, ...]
    cascade_min_tokens_in: int = Field(ge=0)
    cascade_min_cost_usd: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_evaluations(self) -> Self:
        cases = {case.case_id: case for case in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("case_id must be unique")
        if len({(rung.rung_id, rung.mode) for rung in self.rungs}) != len(self.rungs):
            raise ValueError("duplicate (rung_id, mode)")
        for evaluation in (self.baseline, self.control_without_events, *self.rungs):
            for run in evaluation.runs:
                case = cases.get(run.case_id)
                if case is None:
                    raise ValueError(f"unknown case_id: {run.case_id}")
                allowed = set(case.labelled_item_ids) | set(case.negative_control_ids)
                if set(run.recalled_item_ids) - allowed:
                    raise ValueError(
                        f"recalled_item_ids outside labels and controls for {run.case_id}"
                    )
        for rung in self.rungs:
            if any(run.repeat > self.repeats_required for run in rung.runs):
                raise ValueError("rung repeat exceeds repeats_required")
        for name, evaluation in (
            ("baseline", self.baseline),
            ("control_without_events", self.control_without_events),
        ):
            if set(cases) - {run.case_id for run in evaluation.runs}:
                raise ValueError(f"{name} must have a run for every case")
        return self


class ModelGateFailure(_Frozen):
    metric: EnumGateMetric
    case_id: str = Field(min_length=1)
    repeat: int | None
    detail: str


class ModelLatency(_Frozen):
    p50_ms: int
    p95_ms: int
    max_ms: int


class ModelRungCost(_Frozen):
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency: ModelLatency


class ModelRungPassed(_Frozen):
    rung_id: str
    served_model_id: str
    mode: Literal["whole", "chunked"]
    cost: ModelRungCost
    parse_failures: int
    recall: float
    tail_summary_scored: int
    tail_summary_matched: int


class ModelRungFailed(_Frozen):
    rung_id: str
    served_model_id: str
    mode: Literal["whole", "chunked"]
    failures: tuple[ModelGateFailure, ...] = Field(min_length=1)
    parse_failures: int


class ModelRoutingPromotion(_Frozen):
    task_class: str
    rung_id: str
    served_model_id: str
    mode: Literal["whole", "chunked"]
    change: Literal["routing_contract_entry"] = "routing_contract_entry"
    separate_pr: Literal[True] = True


class ModelRoleAdoptionVerdictResult(_Frozen):
    evaluation_id: str
    task_class: str
    outcome: EnumAdoptionOutcome
    passed: tuple[ModelRungPassed, ...]
    failed: tuple[ModelRungFailed, ...]
    lowest_cost: tuple[str, ...]
    promotions: tuple[ModelRoutingPromotion, ...]
    recommendation: str
    cascade_justified: bool
    cascade_reason: str
