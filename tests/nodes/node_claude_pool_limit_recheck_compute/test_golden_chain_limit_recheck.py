# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: a limited label is probed, the ok answer clears its mark: the golden event chain."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_claude_pool_limit_recheck_compute as node_package
from omnimarket.nodes.node_claude_pool_limit_recheck_compute.handlers.handler_limit_recheck import (
    HandlerLimitRecheck,
)
from omnimarket.nodes.node_claude_pool_limit_recheck_compute.models import (
    ModelLimitRecheckRequest,
    ModelLimitRecheckResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent

BASE = {
    "label": "alpha",
    "fingerprint": "4d2430e0",
    "until": "2026-11-13T11:00:00Z",
    "last_probe_at": None,
    "now": "2026-11-11T23:00:00Z",
    "policy": {"interval_seconds": 3600},
}


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_claude_pool_limit_recheck_compute"
    assert contract["node_type"] == "compute"
    assert contract["lifecycle"] == "experimental"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerLimitRecheck
    assert issubclass(node_package.NodeClaudePoolLimitRecheckCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelLimitRecheckRequest
    assert contract["event_bus"]["subscribe_topics"] == []
    assert contract["event_bus"]["publish_topics"] == []


def test_golden_chain_probe_due_then_ok_answer_clears_the_mark() -> None:
    handler = HandlerLimitRecheck()
    due = handler.handle(ModelLimitRecheckRequest.model_validate_json(json.dumps(BASE)))
    assert due.action == "probe"

    answered = {
        **BASE,
        "probe": {"verdict": "ok", "probed_at": "2026-11-11T23:00:05Z"},
    }
    result = handler.handle(
        ModelLimitRecheckRequest.model_validate_json(json.dumps(answered))
    )
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.expected_until.isoformat() == "2026-11-13T11:00:00+00:00"
    assert result.command.fingerprint == "4d2430e0"

    wire = ModelLimitRecheckResult.model_validate_json(result.model_dump_json())
    assert wire == result
    assert "sk-ant" not in result.model_dump_json()
