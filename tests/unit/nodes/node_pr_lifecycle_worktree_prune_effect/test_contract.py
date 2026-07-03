# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract sanity for node_pr_lifecycle_worktree_prune_effect (OMN-13859)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_lifecycle_worktree_prune_effect"
    / "contract.yaml"
)


@pytest.mark.unit
def test_contract_is_an_effect_node() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    assert data["name"] == "pr_lifecycle_worktree_prune_effect"
    assert data["node_type"] == "effect"
    assert data["descriptor"]["node_archetype"] == "effect"


@pytest.mark.unit
def test_contract_declares_prune_topics() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    assert (
        "onex.cmd.omnimarket.pr-lifecycle-worktree-prune-start.v1"
        in data["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.pr-lifecycle-worktree-prune-completed.v1"
        in data["event_bus"]["publish_topics"]
    )


@pytest.mark.unit
def test_contract_handler_points_at_real_handler() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    assert data["handler"]["class"] == "HandlerWorktreePrune"
    assert (
        data["handler"]["module"]
        == "omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.handler_worktree_prune"
    )


@pytest.mark.unit
def test_only_routable_handler_is_listed_no_adapter_entry() -> None:
    """The git adapter is an injected dependency, not a routable handler.

    OMN-13882 regression: ``GitWorktreeAdapter`` was listed under
    ``handler_routing.handlers`` with no ``event_model`` block. Under the
    ``payload_type_match`` strategy RuntimeLocal rejects every entry lacking
    ``event_model.name``/``.module``, so the adapter entry failed routing
    validation and the node never dispatched. Only the one real
    ``ProtocolMessageHandler`` (``HandlerWorktreePrune``) belongs here.
    """
    data = yaml.safe_load(_CONTRACT.read_text())
    handlers = data["handler_routing"]["handlers"]
    assert len(handlers) == 1, (
        "handler_routing must list exactly the one routable handler; "
        f"adapters/dependencies must not appear here — got {handlers}"
    )
    assert handlers[0]["handler"]["name"] == "HandlerWorktreePrune"


@pytest.mark.unit
def test_every_routing_entry_declares_event_model_for_payload_match() -> None:
    """Every payload_type_match entry must carry a complete event_model.

    Guards against reintroducing a non-routable entry (e.g. an adapter) with a
    missing/partial ``event_model``, which is exactly what OMN-13882 fixed.
    """
    data = yaml.safe_load(_CONTRACT.read_text())
    routing = data["handler_routing"]
    assert routing["routing_strategy"] == "payload_type_match"
    for i, entry in enumerate(routing["handlers"]):
        event_model = entry.get("event_model") or {}
        assert event_model.get("name"), (
            f"handlers[{i}].event_model.name is missing — a non-routable entry "
            "(adapter/dependency) must not be listed under handler_routing"
        )
        assert event_model.get("module"), (
            f"handlers[{i}].event_model.module is missing — a non-routable entry "
            "(adapter/dependency) must not be listed under handler_routing"
        )


@pytest.mark.unit
def test_runtime_local_routing_validation_is_clean() -> None:
    """The exact RuntimeLocal gate that failed closed before OMN-13882.

    Before the fix this returned ``handlers[1].event_model.name is missing`` /
    ``handlers[1].event_model.module is missing`` and the node never dispatched.
    """
    data = yaml.safe_load(_CONTRACT.read_text())
    event_bus = data.get("event_bus", {}) or {}
    errors = RuntimeLocal._validate_routing(
        data["handler_routing"],
        event_bus.get("subscribe_topics", []) or [],
        event_bus.get("publish_topics", []) or [],
    )
    assert errors == [], f"routing validation must be clean, got: {errors}"
