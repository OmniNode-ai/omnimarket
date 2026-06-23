# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_friction_triage_orchestrator [OMN-12205].

The golden chain verifies:
- contract marks the node as implemented
- typed models are strict (extra="forbid")
- entry point loads
- handler dry-run behavior is deterministic
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

NODE_NAME = "node_friction_triage_orchestrator"
HANDLER_MODULE = (
    "omnimarket.nodes.node_friction_triage_orchestrator"
    ".handlers.handler_friction_triage_orchestrator"
)
HANDLER_CLASS = "HandlerFrictionTriageOrchestrator"
REQUEST_MODULE = (
    "omnimarket.nodes.node_friction_triage_orchestrator"
    ".models.model_friction_triage_request"
)
REQUEST_CLASS = "ModelFrictionTriageRequest"
RESULT_CLASS = "ModelFrictionTriageResult"


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
            "operation": "friction_triage",
            "handler": {
                "name": HANDLER_CLASS,
                "module": HANDLER_MODULE,
            },
        }
    ]
    eb = raw["event_bus"]
    assert eb["consumer_group"] == "omnimarket.friction_triage_orchestrator.consume.v1"
    assert "onex.cmd.omnimarket.friction-triage-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.friction-triage-completed.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.friction-triage.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{NODE_NAME}"


@pytest.mark.unit
def test_request_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelFrictionTriageRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806

    req = ModelFrictionTriageRequest(
        friction_registry_path="/tmp/friction.ndjson",
    )
    assert req.friction_registry_path == "/tmp/friction.ndjson"
    assert req.window_days == 30
    assert req.threshold_count == 3
    assert req.threshold_score == 9
    assert req.dry_run is False

    with pytest.raises(ValidationError):
        ModelFrictionTriageRequest(
            friction_registry_path="/tmp/friction.ndjson",
            unexpected_field=True,
        )


@pytest.mark.unit
def test_result_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelFrictionTriageResult = getattr(mod, RESULT_CLASS)  # noqa: N806

    result = ModelFrictionTriageResult(
        surfaces_tracked=5,
        threshold_crossings=2,
        tickets_created=1,
        tickets_skipped=1,
        dry_run=False,
    )
    assert result.surfaces_tracked == 5
    assert result.tickets_created == 1
    assert result.created_ticket_ids == ()

    with pytest.raises(ValidationError):
        ModelFrictionTriageResult(
            surfaces_tracked=5,
            threshold_crossings=2,
            tickets_created=1,
            tickets_skipped=1,
            dry_run=False,
            unexpected_field=True,
        )


@pytest.mark.unit
def test_handler_dry_run_aggregates_threshold_crossings(tmp_path: Path) -> None:
    mod = import_module(HANDLER_MODULE)
    HandlerFrictionTriageOrchestrator = getattr(mod, HANDLER_CLASS)  # noqa: N806

    req_mod = import_module(REQUEST_MODULE)
    ModelFrictionTriageRequest = getattr(req_mod, REQUEST_CLASS)  # noqa: N806

    registry = tmp_path / "friction.ndjson"
    registry.write_text(
        "\n".join(
            [
                '{"skill":"ticketing","surface":"linear","severity_score":3}',
                '{"skill":"ticketing","surface":"linear","severity_score":3}',
                '{"skill":"ticketing","surface":"linear","severity_score":3}',
                '{"skill":"other","surface":"docs","severity_score":1}',
            ]
        ),
        encoding="utf-8",
    )

    handler = HandlerFrictionTriageOrchestrator()
    request = ModelFrictionTriageRequest(
        friction_registry_path=str(registry),
        dry_run=True,
    )

    result = handler.handle(request)

    assert result.surfaces_tracked == 2
    assert result.threshold_crossings == 1
    assert result.tickets_created == 0
    assert result.dry_run is True
