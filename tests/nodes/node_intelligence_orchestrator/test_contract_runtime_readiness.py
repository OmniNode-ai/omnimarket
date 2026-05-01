# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime profile guards for intelligence-owned market contracts."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _load_contract(node_name: str) -> dict[str, Any]:
    with (NODES_DIR / node_name / "contract.yaml").open() as handle:
        contract = yaml.safe_load(handle)
    assert isinstance(contract, dict)
    return contract


def test_intelligence_orchestrator_is_not_main_runtime_owned() -> None:
    contract = _load_contract("node_intelligence_orchestrator")

    assert contract["runtime_profiles"] == ["intelligence"]
    assert "main" not in contract["runtime_profiles"]


def test_intent_event_consumer_is_memory_runtime_owned() -> None:
    contract = _load_contract("node_intent_event_consumer_effect")

    assert contract["runtime_profiles"] == ["memory"]
    assert "main" not in contract["runtime_profiles"]
    assert contract["handler"]["class"] == "HandlerIntentEventConsumer"


def test_intelligence_orchestrator_handler_entries_have_autowiring_names() -> None:
    contract = _load_contract("node_intelligence_orchestrator")
    handlers = contract["handler_routing"]["handlers"]

    assert contract["runtime_profiles"] == ["intelligence"]
    for entry in handlers:
        handler_ref = entry["handler"]
        assert handler_ref["name"]
        assert handler_ref["name"] == handler_ref["function"]
        assert "main" not in contract["runtime_profiles"]


def test_intelligence_orchestrator_function_handlers_resolve() -> None:
    contract = _load_contract("node_intelligence_orchestrator")

    for entry in contract["handler_routing"]["handlers"]:
        handler_ref = entry["handler"]
        module = importlib.import_module(handler_ref["module"])

        assert getattr(module, handler_ref["name"]) is getattr(
            module, handler_ref["function"]
        )
