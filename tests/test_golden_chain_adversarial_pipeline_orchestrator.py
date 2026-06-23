# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_adversarial_pipeline_orchestrator [OMN-12215]."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_adversarial_pipeline_orchestrator.handlers.handler_adversarial_pipeline_orchestrator import (
    HandlerAdversarialPipelineOrchestrator,
)
from omnimarket.nodes.node_adversarial_pipeline_orchestrator.models.model_adversarial_pipeline_request import (
    ModelAdversarialPipelineRequest,
)

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


class FakeDesignAdapter:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def create_plan(self, payload: dict[str, Any]) -> dict[str, str]:
        self.payloads.append(payload)
        return {"plan_path": "/tmp/native-plan.md"}


class FakeReviewAdapter:
    def __init__(self, findings_count: int) -> None:
        self.findings_count = findings_count
        self.payloads: list[dict[str, Any]] = []

    def review_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "findings_count": self.findings_count,
            "findings_summary": f"{self.findings_count} findings",
        }


class FakeTicketAdapter:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def create_tickets(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "created_ticket_ids": ("OMN-1", "OMN-2"),
            "tickets_created": 2,
            "epic_url": "https://linear.app/omninode/issue/OMN-EPIC",
        }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract_path() -> Path:
    return _repo_root() / "src" / "omnimarket" / "nodes" / NODE_NAME / "contract.yaml"


@pytest.mark.unit
def test_contract_marks_node_implemented() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is False
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
            "operation": "adversarial_pipeline",
            "handler": {
                "name": HANDLER_CLASS,
                "module": HANDLER_MODULE,
            },
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
def test_handler_runs_all_stages_through_adapters() -> None:
    design = FakeDesignAdapter()
    review = FakeReviewAdapter(findings_count=4)
    tickets = FakeTicketAdapter()

    result = HandlerAdversarialPipelineOrchestrator(
        design_adapter=design,
        review_adapter=review,
        ticket_adapter=tickets,
    ).handle(
        ModelAdversarialPipelineRequest(
            topic="design a unified auth layer",
            min_findings_gate=3,
            linear_project="Tech Debt Remediation",
        )
    )

    assert result.plan_path == "/tmp/native-plan.md"
    assert result.findings_count == 4
    assert result.gate_passed is True
    assert result.created_ticket_ids == ("OMN-1", "OMN-2")
    assert result.tickets_created == 2
    assert result.stage_reached == 3
    assert design.payloads[0]["linear_project"] == "Tech Debt Remediation"
    assert review.payloads[0]["plan_path"] == "/tmp/native-plan.md"
    assert tickets.payloads[0]["findings_count"] == 4


@pytest.mark.unit
def test_handler_skips_design_when_plan_path_supplied() -> None:
    design = FakeDesignAdapter()
    review = FakeReviewAdapter(findings_count=2)

    result = HandlerAdversarialPipelineOrchestrator(
        design_adapter=design,
        review_adapter=review,
    ).handle(
        ModelAdversarialPipelineRequest(
            topic="review existing plan",
            plan_path="/tmp/existing.md",
            min_findings_gate=3,
        )
    )

    assert result.plan_path == "/tmp/existing.md"
    assert result.gate_passed is False
    assert result.stage_reached == 2
    assert design.payloads == []


@pytest.mark.unit
def test_handler_dry_run_gate_passes_without_ticket_adapter() -> None:
    result = HandlerAdversarialPipelineOrchestrator(
        design_adapter=FakeDesignAdapter(),
        review_adapter=FakeReviewAdapter(findings_count=3),
    ).handle(
        ModelAdversarialPipelineRequest(
            topic="dry run",
            min_findings_gate=3,
            dry_run=True,
        )
    )

    assert result.gate_passed is True
    assert result.dry_run is True
    assert result.stage_reached == 3
    assert result.tickets_created == 0
    assert result.created_ticket_ids == ()


@pytest.mark.unit
def test_handler_requires_design_adapter_without_plan_path() -> None:
    with pytest.raises(RuntimeError, match="design adapter required"):
        HandlerAdversarialPipelineOrchestrator(
            review_adapter=FakeReviewAdapter(findings_count=3)
        ).handle(ModelAdversarialPipelineRequest(topic="missing design"))


@pytest.mark.unit
def test_handler_requires_review_adapter() -> None:
    with pytest.raises(RuntimeError, match="review adapter required"):
        HandlerAdversarialPipelineOrchestrator(
            design_adapter=FakeDesignAdapter()
        ).handle(ModelAdversarialPipelineRequest(topic="missing review"))


@pytest.mark.unit
def test_handler_requires_ticket_adapter_for_live_gate_pass() -> None:
    with pytest.raises(RuntimeError, match="ticket adapter required"):
        HandlerAdversarialPipelineOrchestrator(
            design_adapter=FakeDesignAdapter(),
            review_adapter=FakeReviewAdapter(findings_count=3),
        ).handle(
            ModelAdversarialPipelineRequest(
                topic="missing tickets",
                min_findings_gate=3,
            )
        )


@pytest.mark.unit
def test_handler_rejects_design_adapter_without_plan_path() -> None:
    class BadDesignAdapter:
        def create_plan(self, payload: dict[str, Any]) -> dict[str, str]:
            return {}

    with pytest.raises(RuntimeError, match="did not return plan_path"):
        HandlerAdversarialPipelineOrchestrator(
            design_adapter=BadDesignAdapter(),
            review_adapter=FakeReviewAdapter(findings_count=3),
            ticket_adapter=FakeTicketAdapter(),
        ).handle(
            ModelAdversarialPipelineRequest(
                topic="bad design",
                min_findings_gate=3,
            )
        )


@pytest.mark.unit
def test_handler_gate_failure_does_not_create_tickets() -> None:
    tickets = FakeTicketAdapter()

    result = HandlerAdversarialPipelineOrchestrator(
        design_adapter=FakeDesignAdapter(),
        review_adapter=FakeReviewAdapter(findings_count=1),
        ticket_adapter=tickets,
    ).handle(
        ModelAdversarialPipelineRequest(
            topic="gate fail",
            min_findings_gate=3,
        )
    )

    assert result.gate_passed is False
    assert result.stage_reached == 2
    assert tickets.payloads == []
