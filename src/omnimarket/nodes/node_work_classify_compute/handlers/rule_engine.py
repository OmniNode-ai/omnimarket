# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""First-match rule application over the rubric's data (OMN-22263). No id, token or number lives here."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import NamedTuple

from omnimarket.nodes.node_work_classify_compute.models import ModelRuleInputs
from omnimarket.nodes.node_work_classify_compute.models.model_work_rubric import (
    ModelRuleMatch,
    ModelRunStyleRules,
    ModelWorkTypeRubric,
)


class RuleDecision(NamedTuple):
    """The work-type id and the id of the rule (or classifier) that decided it."""

    work_type: str
    rule_id: str


def lane_tokens(rubric: ModelWorkTypeRubric, lane_name: str | None) -> frozenset[str]:
    """Split a lane name on the rubric's separators."""
    if not lane_name:
        return frozenset()
    pattern = "|".join(re.escape(sep) for sep in rubric.tokenizer.separators)
    return frozenset(part for part in re.split(pattern, lane_name.lower()) if part)


def _matches(
    match: ModelRuleMatch, inputs: ModelRuleInputs, tokens: frozenset[str]
) -> bool:
    checks: list[bool] = []
    if match.lane_tokens:
        checks.append(bool(tokens & set(match.lane_tokens)))
    if match.ticket_class:
        checks.append(inputs.ticket_class in match.ticket_class)
    if match.route_dest:
        checks.append(inputs.route_dest in match.route_dest)
    if match.brief_words:
        checks.append(
            bool(inputs.brief_words and inputs.brief_words & set(match.brief_words))
        )
    return bool(checks) and all(checks)


def decide_by_rules(
    rubric: ModelWorkTypeRubric, inputs: ModelRuleInputs
) -> RuleDecision | None:
    """The first rule that matches, or None. A rule that declares no input never matches."""
    tokens = lane_tokens(rubric, inputs.lane_name)
    for rule in rubric.rules:
        if _matches(rule.match, inputs, tokens):
            return RuleDecision(rule.work_type, rule.id)
    return None


def decide_by_classifier(
    rubric: ModelWorkTypeRubric,
    classifier_outputs: Mapping[str, str],
    admitted: Collection[str],
) -> RuleDecision | None:
    """The first admitted classifier whose label maps to a work type. Excluded classifiers are never read."""
    for classifier in rubric.classifier_inputs:
        if classifier.id not in admitted:
            continue
        label = classifier_outputs.get(classifier.id)
        if label is not None and label in classifier.maps:
            return RuleDecision(classifier.maps[label], f"{classifier.id}")
    return None


def decide_run_style(
    rules: ModelRunStyleRules, signals: Mapping[str, str]
) -> str | None:
    """The first style whose every declared signal holds, or None."""
    for rule in rules.styles:
        if rule.match and all(
            signals.get(name) in values for name, values in rule.match.items()
        ):
            return rule.style
    return None


__all__ = [
    "RuleDecision",
    "decide_by_classifier",
    "decide_by_rules",
    "decide_run_style",
    "lane_tokens",
]
