# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_adversarial_pipeline_orchestrator [OMN-12215].

This node is intentionally not implemented yet. The golden chain verifies:
- contract marks the node as not implemented
- typed models are strict (extra="forbid")
- entry point loads
- handler fails loudly with NotImplementedError
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

NODE_NAME = "node_adversarial_pipeline_orchestrator"
HANDLER_MODULE = (
    "omnimarket.nodes.node_adversarial_pipeline_orchestrator"
    ".handlers.handler_adversarial_pipeline_orchestrator"
)
HANDLER_CLASS = "HandlerAdversarialPipelineOrchestrator"
REQUEST_MODULE = (
    "omnimarket.nodes.node_adversarial_pipeline_orchestrator"
    ".models.model_adversarial_pipeline_request"
)
REQUEST_CLASS = "ModelAdversarialPipelineRequest"
RESULT_CLASS = "ModelAdversarialPipelineResult"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract_path() -> Path:
    return _repo_root() / "src" / "omnimarket" / "nodes" / NODE_NAME / "contract.yaml"


@pytest.mark.unit
def test_contract_marks_node_not_implemented() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is True
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == HANDLER_MODULE
    assert raw["handler"]["class"] == HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{REQUEST_MODULE}.{REQUEST_CLASS}"


@pytest.mark.unit
def test_contract_declares_event_bus_surfaces() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "handler": {
                "name": HANDLER_CLASS,
                "module": HANDLER_MODULE,
            }
        }
    ]
    eb = raw["event_bus"]
    assert (
        eb["consumer_group"]
        == "omnimarket.adversarial_pipeline_orchestrator.consume.v1"
    )
    assert "onex.cmd.omnimarket.adversarial-pipeline-start.v1" in eb["subscribe_topics"]
    assert (
        "onex.evt.omnimarket.adversarial-pipeline-completed.v1" in eb["publish_topics"]
    )
    assert "onex.dlq.omnimarket.adversarial-pipeline.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{NODE_NAME}"


@pytest.mark.unit
def test_request_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelAdversarialPipelineRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806

    req = ModelAdversarialPipelineRequest(
        topic="design a cross-repo dependency analyzer"
    )
    assert req.topic == "design a cross-repo dependency analyzer"
    assert req.min_findings_gate == 3
    assert req.dry_run is False
    assert req.no_launch is False
    assert req.plan_path is None
    assert req.linear_project is None

    with pytest.raises(ValidationError):
        ModelAdversarialPipelineRequest(
            topic="design a cross-repo dependency analyzer",
            unexpected_field=True,
        )


@pytest.mark.unit
def test_result_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelAdversarialPipelineResult = getattr(mod, RESULT_CLASS)  # noqa: N806

    result = ModelAdversarialPipelineResult(
        gate_passed=False,
        dry_run=False,
    )
    assert result.gate_passed is False
    assert result.findings_count == 0
    assert result.tickets_created == 0
    assert result.created_ticket_ids == ()
    assert result.stage_reached == 1

    with pytest.raises(ValidationError):
        ModelAdversarialPipelineResult(
            gate_passed=False,
            dry_run=False,
            unexpected_field=True,
        )


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    mod = import_module(HANDLER_MODULE)
    HandlerAdversarialPipelineOrchestrator = getattr(mod, HANDLER_CLASS)  # noqa: N806

    req_mod = import_module(REQUEST_MODULE)
    ModelAdversarialPipelineRequest = getattr(req_mod, REQUEST_CLASS)  # noqa: N806

    handler = HandlerAdversarialPipelineOrchestrator()
    request = ModelAdversarialPipelineRequest(
        topic="design a unified auth layer",
    )

    with pytest.raises(NotImplementedError, match="OMN-12215"):
        handler.handle(request)
