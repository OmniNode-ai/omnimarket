# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the rubric and run-style rules and measure them on the labelled sample (OMN-22263).

Every bar, id set and class-name set arrives as an argument: the caller reads them from the
overlay keys and the enum. A refusal names the offending id, rule or classifier.
"""

from __future__ import annotations

from collections.abc import Collection

import yaml

from omnimarket.nodes.node_work_classify_compute.handlers.rule_engine import (
    decide_by_rules,
)
from omnimarket.nodes.node_work_classify_compute.models import (
    ModelClassifierPrecision,
    ModelLabelledSample,
    ModelRubricEvaluation,
    ModelRuleInputs,
    ModelRulePrecision,
    ModelRunStyleRules,
    ModelSampleUnit,
    ModelWorkTypeRubric,
)


class RubricLoadError(ValueError):
    """The rubric, the style rules or their measurement fail a load check."""


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def load_rubric(
    text: str,
    *,
    expected_ids: Collection[str],
    task_class_names: Collection[str],
) -> ModelWorkTypeRubric:
    """Parse the rubric and check it against the enum's ids and the task-class contracts."""
    rubric = ModelWorkTypeRubric.model_validate(yaml.safe_load(text))
    declared = set(rubric.work_types)
    expected = set(expected_ids)
    lacking_in_enum = sorted(declared - expected)
    if lacking_in_enum:
        raise RubricLoadError(
            f"the rubric declares work-type ids the enum lacks: {', '.join(lacking_in_enum)}"
        )
    lacking_in_rubric = sorted(expected - declared)
    if lacking_in_rubric:
        raise RubricLoadError(
            f"the enum has ids the rubric does not declare: {', '.join(lacking_in_rubric)}"
        )
    if rubric.undetermined_id not in declared:
        raise RubricLoadError(
            f"undetermined_id {rubric.undetermined_id!r} is not declared"
        )
    known_classes = set(task_class_names)
    for work_type, entry in rubric.work_types.items():
        unknown = sorted(set(entry.task_classes) - known_classes)
        if unknown:
            raise RubricLoadError(
                f"work type {work_type} names task classes the contracts lack: {', '.join(unknown)}"
            )
    for rule in rubric.rules:
        if rule.work_type not in declared:
            raise RubricLoadError(
                f"rule {rule.id} names the undeclared work type {rule.work_type}"
            )
        if rule.work_type == rubric.undetermined_id:
            raise RubricLoadError(f"rule {rule.id} decides {rule.work_type}")
    for classifier in rubric.classifier_inputs:
        for label, mapped in classifier.maps.items():
            if mapped not in declared:
                raise RubricLoadError(
                    f"classifier {classifier.id} maps {label} to the undeclared work type {mapped}"
                )
    return rubric


def load_run_style_rules(
    text: str, *, expected_styles: Collection[str]
) -> ModelRunStyleRules:
    """Parse the run-style rules; a style the enum lacks, or an enum style with no rule, is refused."""
    rules = ModelRunStyleRules.model_validate(yaml.safe_load(text))
    declared = {rule.style for rule in rules.styles}
    expected = set(expected_styles)
    if declared - expected:
        raise RubricLoadError(
            f"run-style rules name styles the enum lacks: {', '.join(sorted(declared - expected))}"
        )
    if expected - declared:
        raise RubricLoadError(
            f"the enum has styles with no rule: {', '.join(sorted(expected - declared))}"
        )
    return rules


def _inputs(unit: ModelSampleUnit) -> ModelRuleInputs:
    return ModelRuleInputs(
        lane_name=unit.lane_name,
        ticket_class=unit.ticket_class,
        route_dest=unit.route_dest,
        brief_words=unit.brief_words,
    )


def evaluate_rubric(
    rubric: ModelWorkTypeRubric,
    sample: ModelLabelledSample,
    *,
    precision_bar: float,
) -> ModelRubricEvaluation:
    """Measure each deciding rule, the rule set and each existing classifier on the sample."""
    fired: dict[str, int] = {rule.id: 0 for rule in rubric.rules}
    correct: dict[str, int] = dict(fired)
    decided = 0
    right = 0
    for unit in sample.units:
        decision = decide_by_rules(rubric, _inputs(unit))
        if decision is None:
            continue
        decided += 1
        fired[decision.rule_id] += 1
        if decision.work_type == unit.label:
            correct[decision.rule_id] += 1
            right += 1
    classifier_results: list[ModelClassifierPrecision] = []
    for classifier in rubric.classifier_inputs:
        predicted = 0
        hits = 0
        for unit in sample.units:
            label = unit.classifier_outputs.get(classifier.id)
            if label is None or label not in classifier.maps:
                continue
            predicted += 1
            if classifier.maps[label] == unit.label:
                hits += 1
        precision = _ratio(hits, predicted)
        classifier_results.append(
            ModelClassifierPrecision(
                classifier_id=classifier.id,
                predicted=predicted,
                correct=hits,
                precision=precision,
                admitted=precision is not None and precision >= precision_bar,
            )
        )
    return ModelRubricEvaluation(
        units_total=len(sample.units),
        decided=decided,
        correct=right,
        precision_overall=_ratio(right, decided),
        decided_share=_ratio(decided, len(sample.units)),
        rule_results=tuple(
            ModelRulePrecision(
                rule_id=rule.id,
                fired=fired[rule.id],
                correct=correct[rule.id],
                precision=_ratio(correct[rule.id], fired[rule.id]),
            )
            for rule in rubric.rules
        ),
        classifier_results=tuple(classifier_results),
    )


def load_measured_rubric(
    text: str,
    sample_text: str,
    *,
    expected_ids: Collection[str],
    task_class_names: Collection[str],
    precision_bar: float,
) -> tuple[ModelWorkTypeRubric, ModelRubricEvaluation]:
    """Load the rubric and refuse it unless the rule set and every kept rule reach the bar."""
    rubric = load_rubric(
        text, expected_ids=expected_ids, task_class_names=task_class_names
    )
    sample = ModelLabelledSample.model_validate_json(sample_text)
    evaluation = evaluate_rubric(rubric, sample, precision_bar=precision_bar)
    for result in evaluation.rule_results:
        if result.precision is None:
            raise RubricLoadError(
                f"rule {result.rule_id} fires on no sample unit, so its precision is unmeasured"
            )
        if result.precision < precision_bar:
            raise RubricLoadError(
                f"rule {result.rule_id} has precision {result.precision:.3f} on the sample, "
                f"under the bar {precision_bar}"
            )
    if (
        evaluation.precision_overall is None
        or evaluation.precision_overall < precision_bar
    ):
        raise RubricLoadError(
            f"overall precision {evaluation.precision_overall} on the sample is under the bar "
            f"{precision_bar}"
        )
    return rubric, evaluation


__all__ = [
    "RubricLoadError",
    "evaluate_rubric",
    "load_measured_rubric",
    "load_rubric",
    "load_run_style_rules",
]
