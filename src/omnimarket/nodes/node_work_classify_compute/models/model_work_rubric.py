# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-type rubric and run-style rules as typed data (OMN-22263). Loaded from YAML, never held in code."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRuleMatch(BaseModel):
    """Inputs a rule tests. Every declared input must match; any listed value of one input matches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_tokens: tuple[str, ...] = ()
    ticket_class: tuple[str, ...] = ()
    route_dest: tuple[str, ...] = ()
    brief_words: tuple[str, ...] = ()


class ModelRubricRule(BaseModel):
    """One first-match rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    work_type: str = Field(min_length=1)
    match: ModelRuleMatch


class ModelWorkTypeEntry(BaseModel):
    """One declared work type: its delegation task classes, evidence and outcome words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_classes: tuple[str, ...]
    completion_evidence: str = Field(min_length=1)
    good_outcomes: tuple[str, ...]


class ModelClassifierInput(BaseModel):
    """An existing classifier offered as a rule input, with its label-to-work-type map."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    maps: dict[str, str]


class ModelTokenizer(BaseModel):
    """How a lane name splits into tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    separators: tuple[str, ...] = Field(min_length=1)


class ModelBriefPatterns(BaseModel):
    """Regular expressions that read brief features before a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repositories_named: str
    criteria_line: str
    falsifier: str
    premise_verified: str
    premise_unverified: str
    brief_word: str


class ModelBriefStratum(BaseModel):
    """One brief stratum: all declared limits must hold, null is unbounded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    max_repositories: int | None
    max_length_chars: int | None
    max_criteria: int | None


class ModelCauses(BaseModel):
    """Causes a unit stays undetermined."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    no_intent_text: str
    no_rule: str
    no_route: str
    route_invalid: str


class ModelWorkTypeRubric(BaseModel):
    """work_type_rubric.v1.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rubric_version: str = Field(min_length=1)
    undetermined_id: str = Field(min_length=1)
    work_types: dict[str, ModelWorkTypeEntry]
    rules: tuple[ModelRubricRule, ...]
    classifier_inputs: tuple[ModelClassifierInput, ...]
    tokenizer: ModelTokenizer
    brief_patterns: ModelBriefPatterns
    brief_strata: tuple[ModelBriefStratum, ...] = Field(min_length=1)
    causes: ModelCauses
    lab_route_prompt: str = Field(min_length=1)


class ModelStyleRule(BaseModel):
    """One run-style rule: every declared signal must hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    style: str = Field(min_length=1)
    match: dict[str, tuple[str, ...]]


class ModelRunStyleRules(BaseModel):
    """run_style_rules.v1.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules_version: str = Field(min_length=1)
    styles: tuple[ModelStyleRule, ...] = Field(min_length=1)
    cause_no_style: str = Field(min_length=1)


__all__ = [
    "ModelBriefPatterns",
    "ModelBriefStratum",
    "ModelCauses",
    "ModelClassifierInput",
    "ModelRubricRule",
    "ModelRuleMatch",
    "ModelRunStyleRules",
    "ModelStyleRule",
    "ModelTokenizer",
    "ModelWorkTypeEntry",
    "ModelWorkTypeRubric",
]
