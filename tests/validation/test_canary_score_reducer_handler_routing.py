# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression guard: node_canary_score_reducer must declare handler_routing.

OMN-13691: The deprecated top-level `handler:` schema was used instead of
canonical `handler_routing:`. This test locks the migration so the defect
cannot regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

NODE_NAME = "node_canary_score_reducer"


@pytest.mark.unit
def test_canary_score_reducer_declares_handler_routing() -> None:
    """node_canary_score_reducer/contract.yaml must use handler_routing, not bare handler:.

    Canonical contracts declare handler dispatch via:
      handler_routing:
        routing_strategy: operation_match
        handlers: [...]

    The deprecated bare `handler: {module, class, input_model}` schema must
    not be the sole wiring mechanism in this contract (OMN-13691).
    """
    contract_path = NODES_ROOT / NODE_NAME / "contract.yaml"
    assert contract_path.exists(), f"contract.yaml not found at {contract_path}"

    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)

    routing = raw.get("handler_routing")
    assert routing is not None, (
        f"{NODE_NAME}/contract.yaml is missing `handler_routing:` block. "
        "Migrate from the deprecated `handler:` schema to "
        "`handler_routing: {{routing_strategy: operation_match, handlers: [...]}}` "
        "(OMN-13691)."
    )
    assert isinstance(routing, dict), (
        f"{NODE_NAME}/contract.yaml `handler_routing` must be a mapping, got {type(routing)}"
    )
    assert routing.get("routing_strategy") == "operation_match", (
        f"{NODE_NAME}/contract.yaml handler_routing.routing_strategy must be "
        f"'operation_match', got {routing.get('routing_strategy')!r}"
    )
    handlers = routing.get("handlers")
    assert isinstance(handlers, list), (
        f"{NODE_NAME}/contract.yaml handler_routing.handlers must be a list"
    )
    assert len(handlers) >= 1, (
        f"{NODE_NAME}/contract.yaml handler_routing.handlers must be a non-empty list"
    )
    first = handlers[0]
    assert isinstance(first, dict), "First handler entry must be a mapping"
    assert "operation" in first, (
        "First handler entry must declare an `operation` key "
        f"(OMN-13691); got keys: {list(first.keys())}"
    )
    nested_handler = first.get("handler")
    assert isinstance(nested_handler, dict), (
        "First handler entry must have a nested `handler:` mapping with `name` and `module`"
    )
    assert nested_handler.get("name") == "HandlerCanaryScoreReducer", (
        f"Expected handler name 'HandlerCanaryScoreReducer', got {nested_handler.get('name')!r}"
    )
    expected_module = "omnimarket.nodes.node_canary_score_reducer.handlers.handler_canary_score_reducer"
    assert nested_handler.get("module") == expected_module, (
        f"Expected handler module {expected_module!r}, got {nested_handler.get('module')!r}"
    )


@pytest.mark.unit
def test_handler_routing_scan_finds_no_bare_handler_reducer_missing_routing() -> None:
    """DoD validator: no omnimarket node may have bare handler: without handler_routing:.

    Implements the precise DoD check from OMN-13691: any node contract that declares
    a bare `handler: {module, class, ...}` block AND subscribes to event topics
    MUST also declare `handler_routing:`. The canonical schema requires handler_routing.

    Note: node_e2e_orchestrator uses a standalone consumer.py (no handler: block) and
    therefore falls outside this check. It is tracked separately for full canonicalization.
    """
    violations: list[str] = []
    for contract_path in sorted(NODES_ROOT.glob("*/contract.yaml")):
        text = contract_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            continue
        # Node has a bare handler: block (deprecated form)
        bare_handler = raw.get("handler")
        has_bare_handler = isinstance(bare_handler, dict) and (
            "class" in bare_handler or "module" in bare_handler
        )
        # Node already has the canonical form
        has_routing = "handler_routing" in raw
        # Node subscribes to event bus topics (canonical event_bus.subscribe_topics schema)
        has_subscribe_topics = bool(raw.get("event_bus", {}).get("subscribe_topics"))
        # Flag: bare handler without routing AND the node is an event subscriber
        if has_bare_handler and not has_routing and has_subscribe_topics:
            violations.append(contract_path.parent.name)

    assert not violations, (
        "The following nodes use deprecated bare `handler:` without `handler_routing:` "
        f"and subscribe to event topics (OMN-13691): {violations}\n"
        "Add handler_routing: {routing_strategy: operation_match, handlers: [...]} "
        "matching the existing handler class."
    )
