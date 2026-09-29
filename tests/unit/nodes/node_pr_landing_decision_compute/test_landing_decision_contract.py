# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is a pure definition-B COMPUTE: contract, handler shape, fixed briefs."""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_decision_compute import (
    NodePrLandingDecisionCompute,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    HandlerPrLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingBriefClass,
    EnumLandingEngine,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    BRIEF_INSTRUCTIONS,
    ModelLandingDecision,
    ModelLandingWorkerBrief,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

NODE_DIR = Path(handler_pr_landing_decision.__file__).resolve().parents[1]


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_the_typed_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_pr_landing_decision_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelLandingFacts),
        ("output_model", ModelLandingDecision),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPrLandingDecision
    )


@pytest.mark.unit
def test_handler_is_definition_b() -> None:
    signature = inspect.signature(HandlerPrLandingDecision.handle)
    params = list(signature.parameters.values())
    assert [p.name for p in params] == ["self", "request"]
    assert params[1].annotation in (ModelLandingFacts, "ModelLandingFacts")
    assert signature.return_annotation in (ModelLandingDecision, "ModelLandingDecision")
    assert not inspect.iscoroutinefunction(HandlerPrLandingDecision.handle)
    assert issubclass(NodePrLandingDecisionCompute, HandlerPrLandingDecision)
    decision = HandlerPrLandingDecision().handle(
        ModelLandingFacts(tick=1, observed_at=datetime(2026, 9, 28, tzinfo=UTC))
    )
    assert isinstance(decision, ModelLandingDecision)
    assert decision.actions == ()


@pytest.mark.unit
def test_handler_does_no_io_and_imports_no_envelope() -> None:
    source = Path(handler_pr_landing_decision.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)
    forbidden = {
        "os",
        "subprocess",
        "socket",
        "time",
        "random",
        "requests",
        "httpx",
        "pathlib",
    }
    assert not imported & forbidden, imported & forbidden
    assert "ModelEventEnvelope" not in source
    assert "ModelHandlerOutput" not in source
    assert ".now(" not in source
    assert "utcnow" not in source


@pytest.mark.unit
def test_every_brief_class_has_fixed_text() -> None:
    assert set(BRIEF_INSTRUCTIONS) == set(EnumLandingBriefClass)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    base = {
        "pr": "acme/app#1",
        "head_sha": "a" * 40,
        "lease_id": 1,
        "engine": EnumLandingEngine.CLAUDE_SONNET,
        "deadline_at": now,
    }
    for brief_class, text in BRIEF_INSTRUCTIONS.items():
        ModelLandingWorkerBrief(brief_class=brief_class, instructions=text, **base)
        with pytest.raises(ValidationError):
            ModelLandingWorkerBrief(
                brief_class=brief_class, instructions=text + " and more", **base
            )
    with pytest.raises(ValidationError):
        ModelLandingWorkerBrief(
            brief_class=EnumLandingBriefClass.REAL_RED,
            instructions=BRIEF_INSTRUCTIONS[EnumLandingBriefClass.REAL_RED],
            push_rule="push however you like",
            **base,
        )
