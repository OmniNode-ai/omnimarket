# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19787: validate code-generation golden tasks and independent hidden tests."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_delegation_ground_truth_label_compute
from omnimarket.models.delegation_gate_eval.model_ground_truth_label_request import (
    ModelGroundTruthLabelRequest,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_run import (
    ModelGroundTruthRun,
)
from omnimarket.models.delegation_gate_eval.model_hidden_test_outcome import (
    ModelHiddenTestOutcome,
)
from omnimarket.nodes.node_delegation_ground_truth_label_compute.handlers.handler_ground_truth_label import (
    HandlerGroundTruthLabel,
)

pytestmark = pytest.mark.unit

_DATASET_PATH = (
    Path(omnimarket.nodes.node_delegation_ground_truth_label_compute.__file__).parent
    / "golden"
    / "hidden_test_tasks.v1.yaml"
)
with _DATASET_PATH.open(encoding="utf-8") as _dataset_file:
    _DATASET = yaml.safe_load(_dataset_file)
_TASKS = _DATASET["tasks"]
_TASKS_BY_ID = {task["id"]: task for task in _TASKS}


def _run_hidden_test(
    source: str, hidden_test: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """Execute every hidden case, counting assertions and runtime errors as failures."""
    module = types.ModuleType("solution")
    exec(source, module.__dict__)
    monkeypatch.setitem(sys.modules, "solution", module)
    namespace: dict[str, Any] = {}
    exec(hidden_test, namespace)
    cases = [
        value
        for name, value in namespace.items()
        if name.startswith("test_") and callable(value)
    ]
    passed = 0
    for case in cases:
        try:
            case()
        except (Exception, pytest.fail.Exception):
            continue
        passed += 1
    return passed, len(cases)


@pytest.mark.parametrize("task_id", tuple(_TASKS_BY_ID))
def test_reference_passes_every_hidden_case(
    task_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _TASKS_BY_ID[task_id]
    passed, total = _run_hidden_test(
        task["reference"], task["hidden_test"], monkeypatch
    )
    assert passed == total
    assert 7 <= total <= 10


@pytest.mark.parametrize("task_id", tuple(_TASKS_BY_ID))
def test_control_fails_a_hidden_case(
    task_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _TASKS_BY_ID[task_id]
    passed, total = _run_hidden_test(task["control"], task["hidden_test"], monkeypatch)
    assert 0 < passed < total


@pytest.mark.parametrize("task_id", tuple(_TASKS_BY_ID))
def test_hidden_test_never_appears_in_the_prompt(task_id: str) -> None:
    task = _TASKS_BY_ID[task_id]
    run = ModelGroundTruthRun(
        run_id=task_id,
        task_class="code_generation",
        stratum="hidden_test",
        delegated_model="Qwen3.8-27B",
        prompt_text=task["prompt"],
        hidden_test=ModelHiddenTestOutcome(
            test_id=task_id,
            hidden_test_source=task["hidden_test"],
            artifact_extracted=True,
            cases_total=1,
            cases_passed=1,
        ),
    )
    result = HandlerGroundTruthLabel().handle(
        ModelGroundTruthLabelRequest(batch_id="golden", runs=(run,))
    )
    assert result.labels[0].reason != "hidden_test_leaked_into_prompt"


def test_dataset_has_ten_unique_tasks_with_matching_function_names() -> None:
    assert set(_DATASET) == {"schema_version", "task_class", "tasks"}
    assert _DATASET["schema_version"] == 1
    assert _DATASET["task_class"] == "code_generation"
    assert len(_TASKS) == 10
    assert len(_TASKS_BY_ID) == 10
    for task in _TASKS:
        assert set(task) == {
            "id",
            "function",
            "prompt",
            "hidden_test",
            "reference",
            "control",
        }
        assert task["id"] == task["function"]


def test_checklist_dataset_has_ten_document_tasks_with_checklists() -> None:
    path = _DATASET_PATH.with_name("checklist_tasks.v1.yaml")
    with path.open(encoding="utf-8") as handle:
        dataset = yaml.safe_load(handle)
    assert dataset["task_class"] == "document"
    tasks = dataset["tasks"]
    assert len({task["id"] for task in tasks}) == len(tasks) == 10
    for task in tasks:
        assert set(task) == {"id", "prompt", "checklist"}
        item_ids = [item["id"] for item in task["checklist"]]
        assert len(item_ids) >= 4
        assert len(set(item_ids)) == len(item_ids)
        assert all(item["text"].endswith("?") for item in task["checklist"])
