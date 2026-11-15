# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract.yaml loads, names the handler class and input/output models that import, and the overlay example
validates against ModelReviewGateContextOverlay.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_review_gate_context_compute.overlay import (
    ModelReviewGateContextOverlay,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_review_gate_context_compute"


def test_contract_yaml_loads_and_names_handler_and_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_name"] == "node_review_gate_context_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["handler"]["class"] == "HandlerReviewGateContext"
    handler_module = importlib.import_module(contract["handler"]["module"])
    assert hasattr(handler_module, "HandlerReviewGateContext")
    input_module = importlib.import_module(contract["input_model"]["module"])
    assert hasattr(input_module, contract["input_model"]["name"])
    output_module = importlib.import_module(contract["output_model"]["module"])
    assert hasattr(output_module, contract["output_model"]["name"])


def test_overlay_example_validates() -> None:
    data = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    overlay = ModelReviewGateContextOverlay.model_validate(data)
    assert overlay.policy.check_name == "review-verdict / clean-head"
    assert len(overlay.policy.bypass_classes) == 2
