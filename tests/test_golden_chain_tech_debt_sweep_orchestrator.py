# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_tech_debt_sweep_orchestrator [OMN-12212].

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

NODE_NAME = "node_tech_debt_sweep_orchestrator"
HANDLER_MODULE = (
    "omnimarket.nodes.node_tech_debt_sweep_orchestrator"
    ".handlers.handler_tech_debt_sweep_orchestrator"
)
HANDLER_CLASS = "HandlerTechDebtSweepOrchestrator"
REQUEST_MODULE = (
    "omnimarket.nodes.node_tech_debt_sweep_orchestrator"
    ".models.model_tech_debt_sweep_request"
)
REQUEST_CLASS = "ModelTechDebtSweepRequest"
RESULT_CLASS = "ModelTechDebtSweepResult"


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
    assert eb["consumer_group"] == "omnimarket.tech_debt_sweep_orchestrator.consume.v1"
    assert "onex.cmd.omnimarket.tech-debt-sweep-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.tech-debt-sweep-completed.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.tech-debt-sweep.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{NODE_NAME}"


@pytest.mark.unit
def test_request_model_defaults_and_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelTechDebtSweepRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806

    req = ModelTechDebtSweepRequest()
    assert req.repos == ()
    assert req.categories == ()
    assert req.dry_run is False
    assert req.linear_team == "Omninode"
    assert req.linear_project == "Active Sprint"

    with pytest.raises(ValidationError):
        ModelTechDebtSweepRequest(unexpected_field=True)


@pytest.mark.unit
def test_request_model_accepts_explicit_values() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelTechDebtSweepRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806

    req = ModelTechDebtSweepRequest(
        repos=("omnibase_infra", "omnibase_core"),
        categories=("type-ignore", "noqa"),
        dry_run=True,
    )
    assert req.repos == ("omnibase_infra", "omnibase_core")
    assert req.categories == ("type-ignore", "noqa")
    assert req.dry_run is True


@pytest.mark.unit
def test_result_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelCategoryResult = mod.ModelCategoryResult  # noqa: N806
    ModelTechDebtSweepResult = getattr(mod, RESULT_CLASS)  # noqa: N806

    cat = ModelCategoryResult(
        category="type-ignore",
        total_findings=10,
        new_findings=3,
        already_tracked=7,
        tickets_created=2,
    )
    result = ModelTechDebtSweepResult(
        repos_scanned=("omnibase_infra",),
        category_results=(cat,),
        total_findings=10,
        total_new_findings=3,
        total_tickets_created=2,
        skipped_duplicates=7,
        dry_run=False,
    )
    assert result.total_findings == 10
    assert result.total_tickets_created == 2
    assert result.repos_skipped_stale_ignores == ()
    assert result.summary == ""

    with pytest.raises(ValidationError):
        ModelTechDebtSweepResult(
            repos_scanned=("omnibase_infra",),
            category_results=(cat,),
            total_findings=10,
            total_new_findings=3,
            total_tickets_created=2,
            skipped_duplicates=7,
            dry_run=False,
            unexpected_field=True,
        )


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    mod = import_module(HANDLER_MODULE)
    HandlerTechDebtSweepOrchestrator = getattr(mod, HANDLER_CLASS)  # noqa: N806

    req_mod = import_module(REQUEST_MODULE)
    ModelTechDebtSweepRequest = getattr(req_mod, REQUEST_CLASS)  # noqa: N806

    handler = HandlerTechDebtSweepOrchestrator()
    request = ModelTechDebtSweepRequest()

    with pytest.raises(NotImplementedError, match="OMN-12212"):
        handler.handle(request)
