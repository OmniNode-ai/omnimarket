# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract shape and handler/model resolution for the host hygiene decision compute."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_host_hygiene import (
    ModelHostHygieneDecision,
    ModelHostHygieneDecisionRequest,
)
from omnimarket.nodes.node_host_hygiene_decision_compute import (
    NodeHostHygieneDecisionCompute,
)
from omnimarket.nodes.node_host_hygiene_decision_compute.handlers import (
    handler_host_hygiene_decision,
)
from omnimarket.nodes.node_host_hygiene_decision_compute.handlers.handler_host_hygiene_decision import (
    HandlerHostHygieneDecision,
)

NODE_DIR = Path(handler_host_hygiene_decision.__file__).resolve().parents[1]


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_the_typed_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_host_hygiene_decision_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelHostHygieneDecisionRequest),
        ("output_model", ModelHostHygieneDecision),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerHostHygieneDecision
    )
    assert issubclass(NodeHostHygieneDecisionCompute, HandlerHostHygieneDecision)
