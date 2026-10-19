# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed round-one evaluation inputs and text-free scoring outputs (OMN-21141).

Plan task TE.4 scores recorded rung runs against the TE.1 ground-truth labels
(`tests/fixtures/context_compaction/labels.yaml`). Inputs carry record ids, offsets,
kinds, hashes, grades and costs only; no transcript or package text crosses this node.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumPropositionKind(StrEnum):
    RULING = "ruling"
    CORRECTION = "correction"
    CONSTRAINT = "constraint"
    DECISION = "decision"


class EnumSpanControlKind(StrEnum):
    PARTIAL_QUOTE = "partial_quote"
    HYPOTHETICAL = "hypothetical"
    QUESTION = "question"


class EnumRunMode(StrEnum):
    WHOLE = "whole"
    CHUNKED = "chunked"


class EnumAnswerGrade(StrEnum):
    """UNAVAILABLE means no answer could be graded, such as an uncaptured native summary."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class EnumPositiveControl(StrEnum):
    """MISS_SCORED: every removed label scored as missed. NOT_SCORED is a falsifier."""

    MISS_SCORED = "MISS_SCORED"
    NOT_SCORED = "NOT_SCORED"
    ABSENT = "ABSENT"


class ModelLabelSpan(_Frozen):
    """A half-open code-point span of one chain record; same-content records share offsets."""

    event_id: str = Field(min_length=1)
    same_content_event_ids: tuple[str, ...] = ()
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class ModelLabelledProposition(_Frozen):
    """One TE.1 proposition; `hard_gate` is set where the label carries a T0.2 statement id."""

    label_id: str = Field(min_length=1)
    kind: EnumPropositionKind
    span: ModelLabelSpan
    hard_gate: bool


class ModelSpanControl(_Frozen):
    """A partial quote, hypothetical or question that must never score as `controls`."""

    label_id: str = Field(min_length=1)
    control_kind: EnumSpanControlKind
    controls: str = Field(min_length=1)
    span: ModelLabelSpan


class ModelCorrectedRulingControl(_Frozen):
    """A ruling the operator later corrected: credited only with its correction recalled."""

    label_id: str = Field(min_length=1)
    ruling: str = Field(min_length=1)
    correction: str = Field(min_length=1)


class ModelNegativeSegment(_Frozen):
    label_id: str = Field(min_length=1)
    span: ModelLabelSpan


class ModelCaseLabels(_Frozen):
    case_id: str = Field(min_length=1)
    transcript_tokens: int = Field(ge=1)
    propositions: tuple[ModelLabelledProposition, ...] = Field(min_length=1)
    span_controls: tuple[ModelSpanControl, ...] = ()
    corrected_rulings: tuple[ModelCorrectedRulingControl, ...] = ()
    negative_segments: tuple[ModelNegativeSegment, ...] = ()
    already_done: tuple[str, ...] = ()

    @model_validator(mode="after")
    def references_resolve(self) -> Self:
        ids = [p.label_id for p in self.propositions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate proposition label_id in case {self.case_id}")
        known = set(ids)
        for control in self.span_controls:
            if control.controls not in known:
                raise ValueError(
                    f"control {control.label_id} names unknown {control.controls}"
                )
        for pair in self.corrected_rulings:
            if pair.ruling not in known or pair.correction not in known:
                raise ValueError(
                    f"corrected-ruling control {pair.label_id} names unknown labels"
                )
        return self


class ModelBaselineCase(_Frozen):
    """The native summary and the package without events, answered by the fixed model."""

    case_id: str = Field(min_length=1)
    native_recalled_label_ids: tuple[str, ...] = ()
    native_answer: EnumAnswerGrade
    without_events_answer: EnumAnswerGrade


class ModelAdmittedEvent(_Frozen):
    """One event the verifier admitted, with its widened span."""

    event_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    kind: EnumPropositionKind


class ModelCallCost(_Frozen):
    latency_ms: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class ModelRungRun(_Frozen):
    """One repeat of one rung on one case in one mode, scored from its recordings.

    `removed_label_ids` marks a positive-control run: the rung was given the case with
    those labelled propositions removed, and each must score as a miss. The `replayed_*`
    hashes are the ones recomputed when the run is replayed from its recordings.
    """

    rung_id: str = Field(min_length=1)
    served_model_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    repeat: int = Field(ge=1)
    mode: EnumRunMode
    window_tokens: int = Field(ge=1)
    candidate_id: str = Field(min_length=1)
    assembly_hash: str = Field(min_length=1)
    model_outputs_hash: str = Field(min_length=1)
    final_pack_hash: str = Field(min_length=1)
    body_hash: str = Field(min_length=1)
    replayed_model_outputs_hash: str | None = None
    replayed_final_pack_hash: str | None = None
    replayed_body_hash: str | None = None
    admitted: tuple[ModelAdmittedEvent, ...] = ()
    parse_failures: int = Field(default=0, ge=0)
    answer: EnumAnswerGrade
    proposed_already_done: tuple[str, ...] = ()
    calls: tuple[ModelCallCost, ...] = ()
    removed_label_ids: tuple[str, ...] = ()


class ModelRoundOneEvalRequest(_Frozen):
    cases: tuple[ModelCaseLabels, ...] = Field(min_length=1)
    baselines: tuple[ModelBaselineCase, ...] = ()
    runs: tuple[ModelRungRun, ...] = Field(min_length=1)
    reserve_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def references_resolve(self) -> Self:
        cases = {c.case_id: c for c in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("duplicate case_id")
        seen_baselines: set[str] = set()
        for baseline in self.baselines:
            if baseline.case_id not in cases or baseline.case_id in seen_baselines:
                raise ValueError(
                    f"baseline names unknown or repeated case {baseline.case_id}"
                )
            seen_baselines.add(baseline.case_id)
            labels = {p.label_id for p in cases[baseline.case_id].propositions}
            if not set(baseline.native_recalled_label_ids) <= labels:
                raise ValueError(f"baseline {baseline.case_id} names unknown labels")
        keys: set[tuple[str, str, int, str, tuple[str, ...]]] = set()
        for run in self.runs:
            case = cases.get(run.case_id)
            if case is None:
                raise ValueError(f"run names unknown case {run.case_id}")
            if not set(run.proposed_already_done) <= set(case.already_done):
                raise ValueError(
                    f"run proposes an item outside {run.case_id} already_done"
                )
            if not set(run.removed_label_ids) <= {
                p.label_id for p in case.propositions
            }:
                raise ValueError(f"run removes unknown labels in {run.case_id}")
            key = (
                run.rung_id,
                run.case_id,
                run.repeat,
                run.mode.value,
                tuple(sorted(run.removed_label_ids)),
            )
            if key in keys:
                raise ValueError(f"duplicate run {key}")
            keys.add(key)
        return self


class ModelInterval(_Frozen):
    """A proportion with its 95% Wilson score interval, rounded to 6 places."""

    successes: int = Field(ge=0)
    trials: int = Field(ge=0)
    rate: float
    low: float
    high: float


class ModelControlReport(_Frozen):
    label_id: str
    case_id: str
    control_kind: str
    runs_scored: int
    violations: int


class ModelModeSummary(_Frozen):
    mode: EnumRunMode
    runs: int
    recall: ModelInterval
    hard_gate_met: bool


class ModelCaseRungScore(_Frozen):
    case_id: str
    runs: int
    recall: ModelInterval
    hard_gate_met: bool
    control_violations: int
    modes: tuple[EnumRunMode, ...]
    whole_chunked_disagree: bool
    single_mode_where_both_allowed: bool


class ModelCostSummary(_Frozen):
    calls: int
    latency_p50_ms: int
    latency_p95_ms: int
    latency_max_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


class ModelRungReport(_Frozen):
    rung_id: str
    served_model_ids: tuple[str, ...]
    runs: int
    per_case: tuple[ModelCaseRungScore, ...]
    modes: tuple[ModelModeSummary, ...]
    recall: ModelInterval
    recall_not_below_native: bool
    hard_gate_met: bool
    controls: tuple[ModelControlReport, ...]
    precision: ModelInterval
    negative_segment_admissions: int
    parse_failures: int
    next_action: ModelInterval
    next_action_met: bool
    duplicate_work_runs: int
    no_duplicate_work: bool
    positive_control: EnumPositiveControl
    passes_metrics_1_to_3: bool
    cost: ModelCostSummary | None
    zero_metrics: tuple[str, ...]


class ModelRoundOneEvalResult(_Frozen):
    rungs: tuple[ModelRungReport, ...]
    native_recall: ModelInterval
    native_next_action: ModelInterval
    without_events_next_action: ModelInterval
    candidate_id_mismatches: tuple[str, ...]
    assembly_hash_mismatches: tuple[str, ...]
    replay_mismatches: tuple[str, ...]
    unreplayed_runs: int
    eligible_rungs: tuple[str, ...]
    falsifiers: tuple[str, ...]
