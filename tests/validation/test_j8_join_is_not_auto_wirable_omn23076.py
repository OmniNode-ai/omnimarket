# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A handler the auto-wiring cannot construct is not a ``handler_routing`` entry.

OMN-22919 (#5301, on dev at 2026-11-08T03:27:37Z as 7dd22344) listed
``HandlerLandingJoinJ8`` under ``node_pr_landing_decision_compute``'s
``handler_routing`` for an operation named ``join_j8_landing_decision``. The
handler is an EFFECT taking ``sink: ProtocolEmissionSink`` and
``policy: ModelJoinJ8Policy`` through its constructor, and nothing in the
auto-wiring can supply either, so the runtime-ownership guard refused:

    node_pr_landing_decision_compute/HandlerLandingJoinJ8 requires constructor
    dependencies ('sink', 'policy'); declare runtime_profiles or dependency ownership

That shard is ``pull_request``-only, so dev's own head carries no
``Tests (Split N/20)`` row and read green while the red sat on 18 of 52 open
pull requests, omnimarket merged nothing for an hour, and the fleet fell under
its 5/h stall line.

The entry was also dead: ``join_j8_landing_decision`` appeared in that contract
and nowhere else in ``src``, only the J8 unit tests construct the handler, and
the landing controller calls it in process. Worse, an entry declaring no
``event_model`` is granted EVERY contract ``subscribe_topic`` by the
auto-wiring, so it would have been built as an independent consumer of the
node's command topic and thrown on the mismatched payload -- the silent no-op
``node_build_loop_orchestrator`` records in its own contract after six days of a
dead daily trigger.

These two assertions pin the removal rather than the symptom: the operation is
declared nowhere, and the pure decision handler is the node's sole routed
handler. The guard in ``test_runtime_profile_contract_ownership.py`` still
covers the general invariant for every other contract.

Ticket: OMN-23076
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

#: The operation the dead routing entry was declared under.
DEAD_OPERATION = "join_j8_landing_decision"

pytestmark = pytest.mark.unit


def _load(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


def _declared_operations(raw: dict[str, Any]) -> tuple[str, ...]:
    routing = raw.get("handler_routing")
    if not isinstance(routing, dict):
        return ()
    handlers = routing.get("handlers")
    if not isinstance(handlers, list):
        return ()
    return tuple(
        entry["operation"]
        for entry in handlers
        if isinstance(entry, dict) and isinstance(entry.get("operation"), str)
    )


def test_the_dead_operation_is_declared_in_no_contract() -> None:
    """AC2. Nothing can be auto-wired for an operation no contract routes.

    The declaration is read from the parsed ``handler_routing``, never by
    grepping the file: the contract explains in prose why the entry was removed
    and names the operation to do it, and a literal search would be tripped by
    that explanation rather than by a routing entry.
    """
    declaring = [
        contract_path.parent.name
        for contract_path in sorted(NODES_ROOT.glob("*/contract.yaml"))
        if DEAD_OPERATION in _declared_operations(_load(contract_path))
    ]
    assert declaring == [], (
        f"{DEAD_OPERATION} is routed by {declaring}; nothing in src dispatches "
        "it and its handler takes constructor dependencies the auto-wiring "
        "cannot supply"
    )


def test_the_landing_decision_node_routes_only_its_pure_handler() -> None:
    """AC2. One routed handler, so it deterministically owns the node's topics."""
    contract = _load(NODES_ROOT / "node_pr_landing_decision_compute" / "contract.yaml")
    routing = contract.get("handler_routing")
    assert isinstance(routing, dict), contract.keys()

    handlers = routing.get("handlers")
    assert isinstance(handlers, list)
    names = [
        entry.get("handler", {}).get("name")
        for entry in handlers
        if isinstance(entry, dict)
    ]
    assert names == ["HandlerPrLandingDecision"], names
