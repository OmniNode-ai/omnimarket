# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18341: contract-resolved routing request -> compute -> typed result."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter

import omnimarket.nodes.node_routing_complexity_compute as node_package
from omnimarket.nodes.node_routing_complexity_compute.models.model_routing_complexity import (
    ModelRoutingClassification,
    ModelRoutingRefusal,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = Path(node_package.__file__).parent / "contract.yaml"
COUNTEREXAMPLE_PATH = (
    Path(__file__).parent
    / "unit/delegation/fixtures/omn18341_routing_counterexample.json"
)


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")))


def _run_chain(
    payload: dict[str, Any],
) -> ModelRoutingClassification | ModelRoutingRefusal:
    """Resolve the declared models and real handler, then cross JSON boundaries."""
    contract = _contract()
    input_binding = contract["input_model"]
    input_model = getattr(
        importlib.import_module(input_binding["module"]), input_binding["name"]
    )
    request = input_model.model_validate_json(json.dumps(payload))
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert issubclass(node_package.NodeRoutingComplexityCompute, handler_type)
    result = node_package.NodeRoutingComplexityCompute().handle(request)
    output_binding = contract["output_model"]
    output_model = getattr(
        importlib.import_module(output_binding["module"]), output_binding["name"]
    )
    wire_result = TypeAdapter(output_model).validate_json(result.model_dump_json())
    assert isinstance(wire_result, (ModelRoutingClassification, ModelRoutingRefusal))
    assert wire_result == result
    return wire_result


def test_golden_chain_contract_declares_only_runtime_terminal() -> None:
    contract = _contract()
    # This is an assertion of the public channel, never a request event type.
    terminal_topic = contract["terminal_event"]
    assert terminal_topic == "onex.evt.omnimarket.routing-complexity-completed.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal_topic]
    assert contract["externally_consumed_topics"] == [terminal_topic]
    assert "terminal_events" not in contract["runtime_dispatch"]


@pytest.mark.parametrize(
    ("workflow", "output_kind", "verification", "execution_required"),
    [
        ("summarization", "prose", "grounding", False),
        ("test", "code", "test_run", True),
        ("refactor", "patch", "patch_and_test", True),
    ],
)
def test_golden_chain_classification(
    workflow: str, output_kind: str, verification: str, execution_required: bool
) -> None:
    result = _run_chain(
        {"prompt": "Read the rows, then derive the total.", "workflow": workflow}
    )
    assert isinstance(result, ModelRoutingClassification)
    assert result.status == "classified"
    classification = result.classification
    assert classification.workflow == workflow
    assert classification.features.output_kind == output_kind
    assert classification.features.verification == verification
    assert classification.features.execution_required is execution_required
    assert classification.features.dependent_reasoning_steps == 2
    assert classification.score == sum(classification.points.values())
    assert classification.provenance["output_kind"].source == "contract"
    assert (
        classification.provenance["dependent_reasoning_steps"].source
        == "text_measurement"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scorer", "patch_apply_and_test"),
        ("dependent_reasoning_steps", 50),
        ("output_kind", "patch"),
        ("verification", "patch_and_test"),
        ("execution_required", True),
    ],
)
def test_golden_chain_rubric_input_refusal(field: str, value: object) -> None:
    result = _run_chain(
        {"prompt": "Summarise the rows.", "workflow": "summarization", field: value}
    )
    assert isinstance(result, ModelRoutingRefusal)
    assert result.status == "refused"
    assert result.reason == "rubric_input_supplied"
    assert result.fields == (field,)
    assert result.classification is not None
    assert result.classification.features.output_kind == "prose"
    assert result.classification.features.execution_required is False


def test_golden_chain_unknown_workflow_refusal() -> None:
    result = _run_chain({"prompt": "Summarise rows.", "workflow": "not_in_catalogue"})
    assert isinstance(result, ModelRoutingRefusal)
    assert result.status == "refused"
    assert result.reason == "workflow_contract_unavailable"
    assert result.fields == ("workflow",)
    assert result.classification is None


def test_golden_chain_counterexample_preserves_trusted_classification() -> None:
    fixture = json.loads(COUNTEREXAMPLE_PATH.read_text(encoding="utf-8"))
    clean = _run_chain({"prompt": fixture["prompt"], "workflow": fixture["workflow"]})
    assert isinstance(clean, ModelRoutingClassification)
    for scorer, steps in fixture["pairs"]:
        refused = _run_chain(
            {
                "prompt": fixture["prompt"],
                "workflow": fixture["workflow"],
                "scorer": scorer,
                "dependent_reasoning_steps": steps,
            }
        )
        assert isinstance(refused, ModelRoutingRefusal)
        assert refused.status == "refused"
        assert refused.fields == ("scorer", "dependent_reasoning_steps")
        assert refused.classification == clean.classification
