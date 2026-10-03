# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18341: trusted routing features and explicit request refusals."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.complexity import (
    ModelComplexityFeatures,
    load_contract,
)
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.nodes.node_routing_complexity_compute.handlers import (
    handler_routing_complexity as routing,
)
from omnimarket.nodes.node_routing_complexity_compute.handlers.handler_routing_complexity import (
    HandlerRoutingComplexity,
)
from omnimarket.nodes.node_routing_complexity_compute.models.model_routing_complexity import (
    ModelRoutingClassification,
    ModelRoutingRefusal,
    ModelRoutingRequest,
)

pytestmark = pytest.mark.unit

PROMPT = "Summarise the following three ledger rows in one sentence each."
RUBRIC_INPUTS = {
    "scorer": "patch_apply_and_test",
    "dependent_reasoning_steps": 5,
    "output_kind": "patch",
    "verification": "patch_and_test",
    "execution_required": True,
}


def test_contract_derived_features() -> None:
    """AC1: conflicting inputs get refused, with trusted diagnostic features."""
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest(prompt=PROMPT, workflow="summarization", **RUBRIC_INPUTS)
    )
    assert isinstance(result, ModelRoutingRefusal)
    expected = (
        load_task_class_authority().task_classes["summarization"].complexity_contract
    )
    assert expected is not None
    assert result.classification is not None
    features = result.classification.features
    assert features.output_kind == expected.output_kind == "prose"
    assert features.verification == expected.verification == "grounding"
    assert features.execution_required == expected.execution_required is False
    assert set(result.fields) == set(RUBRIC_INPUTS)


def test_step_count_not_caller_controlled() -> None:
    """AC2: different declared counts yield the same trusted diagnostics."""
    handler = HandlerRoutingComplexity()
    results = [
        handler.handle(
            ModelRoutingRequest(
                prompt=PROMPT, workflow="summarization", dependent_reasoning_steps=count
            )
        )
        for count in (1, 50)
    ]
    assert all(isinstance(result, ModelRoutingRefusal) for result in results)
    assert results[0].classification == results[1].classification
    assert all(result.fields == ("dependent_reasoning_steps",) for result in results)


@pytest.mark.parametrize("field", RUBRIC_INPUTS)
@pytest.mark.parametrize("value_kind", ["conflicting", "null", "matching"])
def test_rubric_input_refused(field: str, value_kind: str) -> None:
    """AC3: presence alone refuses, including null and correct-looking values."""
    matching = {
        "scorer": "grounding_rubric",
        "dependent_reasoning_steps": 1,
        "output_kind": "prose",
        "verification": "grounding",
        "execution_required": False,
    }
    value = (
        None
        if value_kind == "null"
        else (matching if value_kind == "matching" else RUBRIC_INPUTS)[field]
    )
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest.model_validate(
            {"prompt": PROMPT, "workflow": "summarization", field: value}
        )
    )
    assert isinstance(result, ModelRoutingRefusal)
    assert result.reason == "rubric_input_supplied"
    assert result.fields == (field,)


@pytest.mark.parametrize("workflow", ["summarization", "test", "refactor"])
def test_feature_provenance(workflow: str) -> None:
    """AC4: each feature records a contract declaration or text measurement."""
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest(
            prompt="Read the code, then derive the result.", workflow=workflow
        )
    )
    assert isinstance(result, ModelRoutingClassification)
    classification = result.classification
    names = set(ModelComplexityFeatures.model_fields) - {"measured", "declared"}
    assert set(classification.provenance) == names
    contract_features = {"output_kind", "verification", "execution_required"}
    assert set(classification.features.declared) == contract_features
    assert set(classification.features.measured) == names - contract_features
    for name, provenance in classification.provenance.items():
        assert provenance.source == (
            "contract" if name in contract_features else "text_measurement"
        )
        assert provenance.reference
        assert provenance.rule
    assert classification.features.dependent_reasoning_steps == 2
    assert classification.points["dependent_reasoning_steps"] == 1


def test_counterexample_fixture() -> None:
    """AC5: the verbatim counterexample cannot change the routed classification."""
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/omn18341_routing_counterexample.json"
        ).read_text()
    )
    assert fixture["prompt"] == PROMPT
    assert fixture["pairs"] == [["grounding_rubric", 1], ["patch_apply_and_test", 5]]
    handler = HandlerRoutingComplexity()
    results = [
        handler.handle(
            ModelRoutingRequest(
                prompt=fixture["prompt"],
                workflow=fixture["workflow"],
                scorer=scorer,
                dependent_reasoning_steps=steps,
            )
        )
        for scorer, steps in fixture["pairs"]
    ]
    assert all(isinstance(result, ModelRoutingRefusal) for result in results)
    assert results[0].classification == results[1].classification
    accepted = handler.handle(
        ModelRoutingRequest(prompt=fixture["prompt"], workflow=fixture["workflow"])
    )
    assert isinstance(accepted, ModelRoutingClassification)
    assert results[0].classification == accepted.classification


def test_unknown_workflow_fails_closed() -> None:
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest(prompt=PROMPT, workflow="not_in_catalogue")
    )
    assert isinstance(result, ModelRoutingRefusal)
    assert result.reason == "workflow_contract_unavailable"
    assert result.fields == ("workflow",)
    assert result.classification is None


def test_text_measurement_is_deterministic_and_contract_governed() -> None:
    handler = HandlerRoutingComplexity()
    request = ModelRoutingRequest(
        prompt="Read rows, then compute totals, after that write the summary.",
        workflow="summarization",
    )
    first = handler.handle(request)
    assert isinstance(first, ModelRoutingClassification)
    assert first == handler.handle(request)
    assert first.classification.features.dependent_reasoning_steps == 3
    assert first.classification.points["dependent_reasoning_steps"] == 2


def test_missing_workflow_declaration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = load_task_class_authority()
    entry = authority.task_classes["summarization"]
    authority.task_classes["summarization"] = entry.model_copy(
        update={"complexity_contract": None}
    )
    monkeypatch.setattr(routing, "load_task_class_authority", lambda: authority)
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest(prompt=PROMPT, workflow="summarization")
    )
    assert isinstance(result, ModelRoutingRefusal)
    assert result.reason == "workflow_contract_unavailable"
    assert result.classification is None


def test_refusal_precedes_unknown_workflow() -> None:
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest(prompt=PROMPT, workflow="unknown", scorer=None)
    )
    assert isinstance(result, ModelRoutingRefusal)
    assert result.reason == "rubric_input_supplied"
    assert result.fields == ("scorer",)


def test_measurement_uses_contract_rule_without_scorer_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rubric = deepcopy(load_contract())
    for name in (
        "output_kind_by_scorer",
        "verification_by_scorer",
        "execution_by_verification",
    ):
        del rubric[name]
    rubric["routing_measurement"]["dependent_reasoning_steps"].update(
        pattern=r"\bNEXT\b", base_steps=2
    )
    monkeypatch.setattr(routing, "load_contract", lambda: rubric)
    handler = HandlerRoutingComplexity()
    # Composition owns a snapshot, even if the loader's cached mapping changes.
    rubric["routing_measurement"]["dependent_reasoning_steps"]["base_steps"] = 99
    result = handler.handle(
        ModelRoutingRequest(
            prompt="Read, then think. NEXT write.", workflow="summarization"
        )
    )
    assert isinstance(result, ModelRoutingClassification)
    assert result.classification.features.dependent_reasoning_steps == 3
    assert result.classification.features.output_kind == "prose"
    assert result.classification.features.verification == "grounding"
    assert result.classification.features.execution_required is False


@pytest.mark.parametrize(
    ("text", "steps"),
    [
        ("authentic another thermal", 1),
        ("THEN after THAT using the result using that result", 5),
        ("then " * 20, 21),
    ],
)
def test_step_markers_and_scoring_cap(text: str, steps: int) -> None:
    result = HandlerRoutingComplexity().handle(
        ModelRoutingRequest(prompt=text, workflow="summarization")
    )
    assert isinstance(result, ModelRoutingClassification)
    assert result.classification.features.dependent_reasoning_steps == steps
    rule = load_contract()["features"]["dependent_reasoning_steps"]
    expected = min((steps - 1) * rule["points_per_step_above_one"], rule["max_points"])
    assert result.classification.points["dependent_reasoning_steps"] == expected


def test_all_catalogue_classes_declare_complexity() -> None:
    authority = load_task_class_authority()
    assert all(
        entry.complexity_contract is not None
        for entry in authority.task_classes.values()
    )


def test_compute_handle_does_not_read_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = HandlerRoutingComplexity()

    def unexpected_read() -> None:
        pytest.fail("compute must use the composition-owned contract snapshot")

    monkeypatch.setattr(routing, "load_contract", unexpected_read)
    monkeypatch.setattr(routing, "load_task_class_authority", unexpected_read)
    result = handler.handle(
        ModelRoutingRequest(prompt=PROMPT, workflow="summarization")
    )
    assert isinstance(result, ModelRoutingClassification)


def test_canonical_compute_scoring_has_no_numeric_policy_literals() -> None:
    node = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_routing_complexity_compute"
    )
    paths = [
        node / "handlers/handler_complexity_scoring.py",
        node / "handlers/handler_routing_complexity.py",
    ]
    permitted = {0, 1, 2, 4}
    for path in paths:
        tree = ast.parse(path.read_text())
        offenders = [
            value.value
            for value in ast.walk(tree)
            if isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
            and value.value not in permitted
        ]
        assert not offenders, f"numeric policy literals in {path}: {offenders}"
