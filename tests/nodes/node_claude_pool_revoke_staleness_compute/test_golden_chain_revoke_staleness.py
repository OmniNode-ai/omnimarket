# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: one revoke request -> HandlerRevokeStaleness -> a stale verdict with a clear."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_claude_pool_revoke_staleness_compute as node_package
from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.handlers.handler_revoke_staleness import (
    HandlerRevokeStaleness,
)
from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.models import (
    ModelRevokeStalenessRequest,
    ModelRevokeStalenessResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent

REQUEST_JSON = json.dumps(
    {
        "label": "alpha",
        "fingerprint": "4d2430e0",
        "revoked_at": "2026-11-02T07:25:20Z",
        "observations": [
            {
                "host": "h053",
                "kind": "limited",
                "at": "2026-11-09T14:07:44Z",
                "fingerprint": "4d2430e0",
            }
        ],
        "policy": {"margin_seconds": 60},
    }
)


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_claude_pool_revoke_staleness_compute"
    assert contract["node_type"] == "compute"
    assert contract["lifecycle"] == "experimental"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerRevokeStaleness
    assert issubclass(node_package.NodeClaudePoolRevokeStalenessCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelRevokeStalenessRequest
    assert contract["event_bus"]["subscribe_topics"] == []
    assert contract["event_bus"]["publish_topics"] == []


def test_golden_chain_json_request_to_stale_verdict_with_clear() -> None:
    request = ModelRevokeStalenessRequest.model_validate_json(REQUEST_JSON)
    result = HandlerRevokeStaleness().handle(request)

    assert result.verdict == "stale"
    assert result.clear is not None
    assert result.clear.expected_revoked_at.isoformat() == "2026-11-02T07:25:20+00:00"
    assert result.clear.evidence_host == "h053"
    assert result.clear.evidence_kind == "limited"
    assert result.clear.fingerprint == "4d2430e0"

    wire = ModelRevokeStalenessResult.model_validate_json(result.model_dump_json())
    assert wire == result
    assert "sk-ant" not in result.model_dump_json()
