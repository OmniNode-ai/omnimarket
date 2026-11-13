# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a challenge page in the closing output makes the mark a challenge: the golden event chain."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_claude_pool_revoke_cause_compute as node_package
from omnimarket.nodes.node_claude_pool_revoke_cause_compute.handlers.handler_revoke_cause import (
    HandlerRevokeCause,
)
from omnimarket.nodes.node_claude_pool_revoke_cause_compute.models import (
    ModelRevokeCauseRequest,
    ModelRevokeCauseResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent

BASE = {
    "label": "alpha",
    "evidence": None,
    "challenge_markers": [],
}


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_claude_pool_revoke_cause_compute"
    assert contract["node_type"] == "compute"
    assert contract["lifecycle"] == "experimental"
    assert contract["metadata"]["related_tickets"] == ["OMN-19998"]
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerRevokeCause
    assert issubclass(node_package.NodeClaudePoolRevokeCauseCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelRevokeCauseRequest
    assert contract["event_bus"]["subscribe_topics"] == []
    assert contract["event_bus"]["publish_topics"] == []


def test_golden_chain_challenge_page_with_a_declared_marker_is_challenged() -> None:
    node = node_package.NodeClaudePoolRevokeCauseCompute()

    marked = node.handle(ModelRevokeCauseRequest.model_validate_json(json.dumps(BASE)))
    assert marked.cause == "revoked"

    evidence = (
        "engine closed after http 403: attention required | cloudflare: "
        "one more step before you reach the site"
    )
    challenged = {
        **BASE,
        "evidence": evidence,
        "challenge_markers": ["ATTENTION REQUIRED | CLOUDFLARE"],
    }
    result = node.handle(
        ModelRevokeCauseRequest.model_validate_json(json.dumps(challenged))
    )
    assert result.cause == "challenged"
    assert result.label == "alpha"

    wire = ModelRevokeCauseResult.model_validate_json(result.model_dump_json())
    assert wire == result
    assert "attention required" not in result.model_dump_json()
