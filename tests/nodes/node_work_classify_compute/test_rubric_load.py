# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1, AC2, AC4 and AC8: the rubric loads against the enum, the task classes and the labelled sample (OMN-22263)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.models.work_classification.enum_work_type import EnumWorkType
from omnimarket.nodes.node_work_classify_compute.handlers.rubric_loader import (
    RubricLoadError,
    load_measured_rubric,
    load_rubric,
)
from omnimarket.nodes.node_work_classify_compute.models import (
    ModelRubricEvaluation,
    ModelWorkTypeRubric,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
RUBRIC = REPO / "src" / "omnimarket" / "contracts" / "work_type_rubric.v1.yaml"
TASK_CLASSES = REPO / "src" / "omnimarket" / "configs" / "task_class_contracts.v1.yaml"
SAMPLE = Path(__file__).parent / "fixtures" / "labelled_units.v1.json"
BAR = 0.8
ENUM_IDS = frozenset(member.value for member in EnumWorkType)


def _rubric_text() -> str:
    return RUBRIC.read_text(encoding="utf-8")


def _task_class_names() -> frozenset[str]:
    document = yaml.safe_load(TASK_CLASSES.read_text(encoding="utf-8"))
    return frozenset(document["task_classes"])


def _load(
    text: str, *, ids: frozenset[str] = ENUM_IDS, bar: float = BAR
) -> tuple[ModelWorkTypeRubric, ModelRubricEvaluation]:
    return load_measured_rubric(
        text,
        SAMPLE.read_text(encoding="utf-8"),
        expected_ids=ids,
        task_class_names=_task_class_names(),
        precision_bar=bar,
    )


def test_the_shipped_rubric_loads_against_the_enum_the_task_classes_and_the_sample() -> (
    None
):
    rubric, evaluation = _load(_rubric_text())
    assert rubric.rubric_version
    assert evaluation.units_total == 50
    assert evaluation.precision_overall is not None
    assert evaluation.precision_overall >= BAR
    assert all(
        r.precision is not None and r.precision >= BAR for r in evaluation.rule_results
    )


def test_a_work_type_id_the_enum_lacks_fails_the_load_naming_the_id() -> None:
    text = _rubric_text().replace(
        "  undetermined:\n",
        "  ghostwork:\n    task_classes: []\n    completion_evidence: none\n    good_outcomes: []\n  undetermined:\n",
    )
    with pytest.raises(RubricLoadError, match="ghostwork"):
        load_rubric(text, expected_ids=ENUM_IDS, task_class_names=_task_class_names())


def test_an_enum_id_the_rubric_does_not_declare_fails_the_load_naming_the_id() -> None:
    with pytest.raises(RubricLoadError, match="newtype"):
        load_rubric(
            _rubric_text(),
            expected_ids=ENUM_IDS | {"newtype"},
            task_class_names=_task_class_names(),
        )


def test_the_enum_and_the_rubric_declare_equal_id_sets() -> None:
    rubric = load_rubric(
        _rubric_text(), expected_ids=ENUM_IDS, task_class_names=_task_class_names()
    )
    assert set(rubric.work_types) == ENUM_IDS


def test_a_task_class_that_is_not_in_the_task_class_contracts_fails_the_load_naming_it() -> (
    None
):
    text = _rubric_text().replace("planning]", "planning, no_such_class]", 1)
    with pytest.raises(RubricLoadError, match="no_such_class"):
        load_rubric(text, expected_ids=ENUM_IDS, task_class_names=_task_class_names())


def test_a_rule_naming_an_undeclared_work_type_fails_the_load_naming_it() -> None:
    text = _rubric_text().replace("work_type: land,", "work_type: nonesuch,", 1)
    with pytest.raises(RubricLoadError, match="nonesuch"):
        load_rubric(text, expected_ids=ENUM_IDS, task_class_names=_task_class_names())


def test_a_planted_wrong_rule_drops_precision_and_the_load_refuses_the_rubric() -> None:
    text = _rubric_text().replace(
        "id: fix-lane-token, work_type: fix", "id: fix-lane-token, work_type: build"
    )
    with pytest.raises(RubricLoadError, match="fix-lane-token"):
        _load(text)


def test_one_wrong_sample_label_is_counted_in_overall_and_in_its_rule_precision() -> (
    None
):
    document = json.loads(SAMPLE.read_text(encoding="utf-8"))
    flipped = next(u for u in document["units"] if u["lane_name"] == "build-fold-node")
    flipped["label"] = "docs"
    rubric, evaluation = load_measured_rubric(
        _rubric_text(),
        json.dumps(document),
        expected_ids=ENUM_IDS,
        task_class_names=_task_class_names(),
        precision_bar=0.5,
    )
    assert rubric.rubric_version
    by_rule = {r.rule_id: r for r in evaluation.rule_results}
    build = by_rule["build-lane-token"]
    assert build.correct == build.fired - 1
    assert evaluation.correct == evaluation.decided - 1
    with pytest.raises(RubricLoadError, match="build-lane-token"):
        load_measured_rubric(
            _rubric_text(),
            json.dumps(document),
            expected_ids=ENUM_IDS,
            task_class_names=_task_class_names(),
            precision_bar=0.99,
        )


def test_a_rule_that_never_fires_on_the_sample_is_unmeasured_and_refused() -> None:
    text = _rubric_text().replace(
        "  - {id: land-lane-token,",
        "  - {id: never-fires, work_type: land, match: {lane_tokens: [zzzz]}}\n  - {id: land-lane-token,",
    )
    with pytest.raises(RubricLoadError, match="never-fires"):
        _load(text)


def test_a_classifier_below_the_bar_is_excluded_and_the_exclusion_is_recorded() -> None:
    _, evaluation = _load(_rubric_text())
    by_id = {c.classifier_id: c for c in evaluation.classifier_results}
    weak = by_id["node_prompt_intent_classify_compute"]
    assert weak.admitted is False
    assert weak.precision is not None
    assert weak.precision < BAR
    strong = by_id["node_ticket_classify_compute"]
    assert strong.admitted is True
    assert strong.precision is not None
    assert strong.precision >= BAR
    unmeasured = by_id["operator_capture_utterance_classifier"]
    assert unmeasured.admitted is False
    assert unmeasured.predicted == 0


def test_the_sample_has_fifty_units_each_with_a_label_the_enum_names() -> None:
    units = json.loads(SAMPLE.read_text(encoding="utf-8"))["units"]
    assert len(units) == 50
    assert {u["label"] for u in units} <= ENUM_IDS
