# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23895: a host-held label outside the store -> HandlerStoreAdopt -> one adopt command: the golden chain."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_claude_pool_store_adopt_compute as node_package
from omnimarket.nodes.node_claude_pool_store_adopt_compute.handlers.handler_store_adopt import (
    HandlerStoreAdopt,
)
from omnimarket.nodes.node_claude_pool_store_adopt_compute.models import (
    ModelStoreAdoptRequest,
    ModelStoreAdoptResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent

REQUEST_JSON = json.dumps(
    {
        "store_tokens": [
            {
                "label": "caia",
                "key": "CLAUDE_CODE_OAUTH_CAIA",
                "fingerprint": "c62464b2",
            }
        ],
        "held": [
            {
                "host": "h053",
                "label": "caia",
                "fingerprint": "c62464b2",
                "revoked": False,
            },
            {
                "host": "h053",
                "label": "onexbot",
                "fingerprint": "4d2430e0",
                "revoked": False,
            },
            {
                "host": "h101",
                "label": "h101-own",
                "fingerprint": "9f000000",
                "revoked": True,
            },
        ],
        "policy": {"key_prefix": "CLAUDE_CODE_OAUTH_"},
    }
)


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_claude_pool_store_adopt_compute"
    assert contract["node_type"] == "compute"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerStoreAdopt
    assert issubclass(node_package.NodeClaudePoolStoreAdoptCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelStoreAdoptRequest
    assert contract["event_bus"]["publish_topics"] == []


def test_golden_chain_json_request_to_one_adopt_command() -> None:
    result = HandlerStoreAdopt().handle(
        ModelStoreAdoptRequest.model_validate_json(REQUEST_JSON)
    )
    assert [(d.label, d.outcome) for d in result.decisions] == [
        ("caia", "in_store"),
        ("h101-own", "revoked_everywhere"),
        ("onexbot", "adopt"),
    ]
    (command,) = result.commands
    assert command.key == "CLAUDE_CODE_OAUTH_ONEXBOT"
    wire = ModelStoreAdoptResult.model_validate_json(result.model_dump_json())
    assert wire == result
    assert "sk-ant" not in result.model_dump_json()
