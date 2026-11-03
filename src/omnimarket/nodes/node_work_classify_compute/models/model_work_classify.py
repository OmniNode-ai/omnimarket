# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and evaluation models of node_work_classify_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_classification.model_work_classification import (
    ModelRouteLine,
    ModelWorkClassification,
)
from omnimarket.nodes.node_work_classify_compute.models.model_work_rubric import (
    ModelRunStyleRules,
    ModelWorkTypeRubric,
)


class ModelBriefFeatures(BaseModel):
    """Features read from a brief before the run. The brief's text is not held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    words: frozenset[str]
    repositories_named: int = Field(ge=0)
    length_chars: int = Field(ge=0)
    criteria_count: int = Field(ge=0)
    falsifier_present: bool
    premises_verified: int = Field(ge=0)
    premises_unverified: int = Field(ge=0)


class ModelBriefExtractRequest(BaseModel):
    """A brief's text and the rubric whose patterns read it. The text is hidden from repr and is not held by the result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(repr=False)
    rubric: ModelWorkTypeRubric


class ModelRuleInputs(BaseModel):
    """The facts first-match rules read. A fact the source lacks is None, not a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_name: str | None
    ticket_class: str | None
    route_dest: str | None
    brief_words: frozenset[str] | None


class ModelWorkClassifyRequest(BaseModel):
    """One unit's facts. Intent text never enters this model; its digest does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    inputs: ModelRuleInputs
    route: ModelRouteLine | None
    brief: ModelBriefFeatures | None
    intent_digest: str | None
    has_intent_text: bool
    signals: dict[str, str]
    classifier_outputs: dict[str, str]
    admitted_classifiers: tuple[str, ...]
    rubric: ModelWorkTypeRubric
    run_style_rules: ModelRunStyleRules


class ModelRulePrecision(BaseModel):
    """Measured precision of one deciding rule on the sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    fired: int = Field(ge=0)
    correct: int = Field(ge=0)
    precision: float | None


class ModelClassifierPrecision(BaseModel):
    """Measured precision of one existing classifier on the sample. A recorded result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classifier_id: str
    predicted: int = Field(ge=0)
    correct: int = Field(ge=0)
    precision: float | None
    admitted: bool


class ModelRubricEvaluation(BaseModel):
    """The rubric measured on the labelled sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    units_total: int = Field(ge=0)
    decided: int = Field(ge=0)
    correct: int = Field(ge=0)
    precision_overall: float | None
    decided_share: float | None
    rule_results: tuple[ModelRulePrecision, ...]
    classifier_results: tuple[ModelClassifierPrecision, ...]


class ModelSampleUnit(BaseModel):
    """One hand-labelled unit of the precision sample."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    unit_id: str = Field(min_length=1)
    lane_name: str | None
    ticket_class: str | None
    route_dest: str | None
    brief_words: frozenset[str]
    label: str = Field(min_length=1)
    classifier_outputs: dict[str, str]


class ModelLabelledSample(BaseModel):
    """labelled_units.v1.json."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    sample_version: str = Field(min_length=1)
    units: tuple[ModelSampleUnit, ...]


class ModelSessionMeasure(BaseModel):
    """One session of a unit: what it ran on and what it spent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    top_level: bool
    carries_brief: bool
    route: ModelRouteLine | None
    model: str | None
    effort: str | None
    tokens: int = Field(ge=0)
    minutes: float = Field(ge=0)
    tool_calls: int = Field(ge=0)


class ModelUnitCellRequest(BaseModel):
    """The sessions of one outcome unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    sessions: tuple[ModelSessionMeasure, ...]


class ModelUnitCell(BaseModel):
    """A unit's cell is its root session's; later and subagent sessions add measures only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str
    root_session_id: str | None
    band: str | None
    model: str | None
    effort: str | None
    tokens: int
    minutes: float
    tool_calls: int
    session_count: int


class ModelReportItem(BaseModel):
    """One classified unit and the calendar month (YYYY-MM) of its window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    month: str = Field(min_length=1)
    classification: ModelWorkClassification


class ModelClassificationReportRequest(BaseModel):
    """The classified units of a window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ModelReportItem, ...]


class ModelMonthShare(BaseModel):
    """The ROUTE-bearing share of one calendar month."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    month: str
    units: int = Field(ge=0)
    route_bearing: int = Field(ge=0)
    share: float | None


class ModelCellCount(BaseModel):
    """Rated units in one (work type, band) cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_type: str
    band: str
    units: int = Field(ge=0)


class ModelStratumCount(BaseModel):
    """Unrated units in one brief stratum. A unit with no stratum is counted under none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stratum: str | None
    units: int = Field(ge=0)


class ModelClassificationReport(BaseModel):
    """The lab-step figures of a window. No unit text is held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: int = Field(ge=0)
    decided_by_rule: int = Field(ge=0)
    decided_by_lab_route: int = Field(ge=0)
    undetermined: int = Field(ge=0)
    rule_share: float | None
    lab_route_share: float | None
    route_bearing_by_month: tuple[ModelMonthShare, ...]
    rated_cells: tuple[ModelCellCount, ...]
    unrated_strata: tuple[ModelStratumCount, ...]


__all__ = [
    "ModelBriefExtractRequest",
    "ModelBriefFeatures",
    "ModelCellCount",
    "ModelClassificationReport",
    "ModelClassificationReportRequest",
    "ModelClassifierPrecision",
    "ModelLabelledSample",
    "ModelMonthShare",
    "ModelReportItem",
    "ModelRubricEvaluation",
    "ModelRuleInputs",
    "ModelRulePrecision",
    "ModelSampleUnit",
    "ModelSessionMeasure",
    "ModelStratumCount",
    "ModelUnitCell",
    "ModelUnitCellRequest",
    "ModelWorkClassifyRequest",
]
