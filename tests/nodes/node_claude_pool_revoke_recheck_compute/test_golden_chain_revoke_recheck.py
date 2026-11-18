# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a revoked label is probed, the ok answer clears its mark: the golden event chain."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_claude_pool_revoke_recheck_compute as node_package
from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.handlers.handler_revoke_recheck import (
    HandlerRevokeRecheck,
)
from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.models import (
    ModelRevokeRecheckRequest,
    ModelRevokeRecheckResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent

BASE = {
    "label": "alpha",
    "fingerprint": "4d2430e0",
    "cause": "revoked",
    "revoked_at": "2026-11-13T11:00:00Z",
    "last_probe_at": None,
    "now": "2026-11-11T23:00:00Z",
    "policy": {"interval_seconds": 3600},
}


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_claude_pool_revoke_recheck_compute"
    assert contract["node_type"] == "compute"
    assert contract["lifecycle"] == "experimental"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerRevokeRecheck
    assert issubclass(node_package.NodeClaudePoolRevokeRecheckCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelRevokeRecheckRequest
    assert contract["event_bus"]["subscribe_topics"] == []
    assert contract["event_bus"]["publish_topics"] == []


def test_golden_chain_probe_due_then_ok_answer_clears_the_mark() -> None:
    handler = HandlerRevokeRecheck()
    due = handler.handle(
        ModelRevokeRecheckRequest.model_validate_json(json.dumps(BASE))
    )
    assert due.action == "probe"

    answered = {
        **BASE,
        "probe": {"verdict": "ok", "probed_at": "2026-11-11T23:00:05Z"},
    }
    result = handler.handle(
        ModelRevokeRecheckRequest.model_validate_json(json.dumps(answered))
    )
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.expected_revoked_at.isoformat() == "2026-11-13T11:00:00+00:00"
    assert result.command.fingerprint == "4d2430e0"

    wire = ModelRevokeRecheckResult.model_validate_json(result.model_dump_json())
    assert wire == result
    assert "sk-ant" not in result.model_dump_json()
