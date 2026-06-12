# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures + live-contract proof for the dispatcher route-coverage gate.

OMN-12880: omnimarket-side fixtures and contract-tree validation.

This file provides two things:

1. Fixture tests that validate the gate's contract parsing logic using
   synthetic YAMLs declared inline — no external dependencies.  These
   are the "fixtures" for the gate (OMN-12880).

2. A live-contract test that scans the actual omnimarket contract tree
   and asserts that every subscribed command topic has handler_routing
   or runtime_dispatch declared.  This is the proof that prevents
   regression to the June 9 DLQ incident shape.

June 9 DLQ regression (would have been caught by this gate):
    node_generation_consumer subscribed onex.cmd.omnimarket.node-generation-requested.v1
    but a sole-handler revert caused zero dispatcher routes to be registered.
    Messages silently went to DLQ.

June 12 DEL-01 live finding (would have been caught by this gate):
    onex.cmd.omnimarket.delegate-skill.v1 was consumed by the dev lane but
    no dispatcher route existed in any deployed contract.
    Discovered via rpk consumer-group lag probe (DEL-01 evidence,
    docs/evidence/2026-06-12-weekend-pass/).

[OMN-12858, OMN-12879, OMN-12880]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CMD_TOPIC_PREFIX = "onex.cmd."

# Known omnimarket command topics that are allowlisted (ratchet model).
# New violations are NEVER silently added here — fix the contract instead.
# Format: topic -> reason | owner | expiry
_ALLOWLISTED_TOPICS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Inline gate logic (avoids cross-repo script import in omnimarket CI)
# ---------------------------------------------------------------------------


def _parse_topics(raw: Any) -> tuple[str, ...]:
    """Parse a subscribe_topics list from a contract YAML value."""
    if not raw:
        return ()
    topics: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            topics.append(entry)
        elif isinstance(entry, dict):
            t = entry.get("topic", "")
            if isinstance(t, str) and t:
                topics.append(t)
    return tuple(topics)


def _has_dispatcher_route(contract_raw: dict[str, Any]) -> bool:
    """Return True if the contract declares handler_routing or runtime_dispatch."""
    return bool(contract_raw.get("handler_routing")) or bool(
        contract_raw.get("runtime_dispatch")
    )


def _scan_contract_dir(root: Path) -> list[dict[str, Any]]:
    """Recursively scan root for contract.yaml files, returning parsed dicts."""
    results = []
    for path in sorted(root.rglob("contract.yaml")):
        try:
            raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        raw["_path"] = path
        results.append(raw)
    return results


# ---------------------------------------------------------------------------
# Synthetic fixture tests
# ---------------------------------------------------------------------------


def _make_contract(
    name: str,
    subscribe_topics: list[str] | None = None,
    compat_topics: list[str] | None = None,
    has_handler_routing: bool = False,
    has_runtime_dispatch: bool = False,
) -> dict[str, Any]:
    """Build a minimal in-memory contract dict for fixture tests."""
    c: dict[str, Any] = {
        "name": name,
        "node_type": "EFFECT_GENERIC",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
    }
    if subscribe_topics is not None:
        c["event_bus"] = {"subscribe_topics": subscribe_topics}
    if compat_topics is not None:
        c["compatibility_publish_topics"] = compat_topics
    if has_handler_routing:
        c["handler_routing"] = {
            "routing_strategy": "operation_match",
            "handlers": [
                {
                    "event_type": "omnimarket.node-generation-requested",
                    "handler": {
                        "name": "HandlerNodeGeneration",
                        "module": "omnimarket.nodes.node_generation_consumer.handlers.handler",
                    },
                }
            ],
        }
    if has_runtime_dispatch:
        c["runtime_dispatch"] = {"strategy": "pattern_b"}
    return c


@pytest.mark.unit
def test_fixture_unrouted_cmd_topic_is_gap() -> None:
    """RED: subscribes cmd topic with no handler_routing => is a gap."""
    contract = _make_contract(
        "node_synthetic_unrouted",
        subscribe_topics=["onex.cmd.omnimarket.node-generation-requested.v1"],
        has_handler_routing=False,
    )

    event_bus: dict[str, Any] = contract.get("event_bus") or {}
    topics = _parse_topics(event_bus.get("subscribe_topics"))
    cmd_topics = [t for t in topics if t.startswith(_CMD_TOPIC_PREFIX)]

    assert len(cmd_topics) == 1
    assert not _has_dispatcher_route(contract), (
        "No handler_routing — this contract is a gap and gate must FAIL"
    )


@pytest.mark.unit
def test_fixture_routed_cmd_topic_is_not_gap() -> None:
    """GREEN: subscribes cmd topic with handler_routing => not a gap."""
    contract = _make_contract(
        "node_synthetic_routed",
        subscribe_topics=["onex.cmd.omnimarket.node-generation-requested.v1"],
        has_handler_routing=True,
    )

    assert _has_dispatcher_route(contract), (
        "handler_routing declared — contract should pass the gate"
    )


@pytest.mark.unit
def test_fixture_runtime_dispatch_covers_cmd_topic() -> None:
    """GREEN: runtime_dispatch satisfies the gate in place of handler_routing."""
    contract = _make_contract(
        "node_pattern_b",
        subscribe_topics=["onex.cmd.omnibase-infra.pattern-b-dispatch.v1"],
        has_runtime_dispatch=True,
    )

    assert _has_dispatcher_route(contract), (
        "runtime_dispatch is a valid dispatcher route declaration"
    )


@pytest.mark.unit
def test_fixture_compat_topic_not_a_gap() -> None:
    """OMN-12880: compatibility_publish_topics are sender-side; never a gap.

    A contract that only declares a compat publish topic must not trigger
    a 'missing dispatcher route' finding — it is a publisher, not a subscriber.
    """
    contract = _make_contract(
        "node_compat_publisher",
        compat_topics=["onex.cmd.omniclaude.task-delegated.v1"],
        has_handler_routing=False,
    )

    # compat topics do NOT appear in event_bus.subscribe_topics
    event_bus: dict[str, Any] = contract.get("event_bus") or {}
    sub_topics = _parse_topics(event_bus.get("subscribe_topics"))
    cmd_sub_topics = [t for t in sub_topics if t.startswith(_CMD_TOPIC_PREFIX)]

    assert len(cmd_sub_topics) == 0, (
        "compat topics must not appear in subscribe_topics; "
        "the gate only checks subscribe_topics for gaps"
    )


@pytest.mark.unit
def test_fixture_evt_topic_not_checked() -> None:
    """Event (evt) topics must not be flagged — gate only checks cmd topics."""
    contract = _make_contract(
        "node_evt_subscriber",
        subscribe_topics=["onex.evt.omnimarket.node-generation-completed.v1"],
        has_handler_routing=False,
    )

    event_bus: dict[str, Any] = contract.get("event_bus") or {}
    topics = _parse_topics(event_bus.get("subscribe_topics"))
    cmd_topics = [t for t in topics if t.startswith(_CMD_TOPIC_PREFIX)]

    # No command topics → gate would report 0 failures for this contract
    assert len(cmd_topics) == 0


@pytest.mark.unit
def test_fixture_delegate_skill_unrouted_matches_del01() -> None:
    """Shape of the June 12 DEL-01 live finding.

    onex.cmd.omnimarket.delegate-skill.v1 was consumed by dev lane but had
    no dispatcher route, causing silent DLQ delivery.
    """
    contract = _make_contract(
        "node_delegation_effect",
        subscribe_topics=["onex.cmd.omnimarket.delegate-skill.v1"],
        has_handler_routing=False,
    )

    event_bus: dict[str, Any] = contract.get("event_bus") or {}
    topics = _parse_topics(event_bus.get("subscribe_topics"))
    cmd_topics = [t for t in topics if t.startswith(_CMD_TOPIC_PREFIX)]

    assert "onex.cmd.omnimarket.delegate-skill.v1" in cmd_topics
    assert not _has_dispatcher_route(contract), (
        "DEL-01 shape: delegate-skill subscribed but no dispatcher route => gap"
    )


# ---------------------------------------------------------------------------
# Live-contract proof: scan actual omnimarket contract tree
# ---------------------------------------------------------------------------

_OMNIMARKET_NODES_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"
)


@pytest.mark.unit
def test_live_omnimarket_contracts_all_routed() -> None:
    """All omnimarket command-topic subscriptions must have a dispatcher route.

    This test is the regression proof for the June 9 DLQ incident and the
    June 12 DEL-01 live finding.  It passes only when the current checked-out
    omnimarket contracts are fully wired.
    """
    if not _OMNIMARKET_NODES_DIR.is_dir():
        pytest.skip(f"omnimarket nodes dir not found: {_OMNIMARKET_NODES_DIR}")

    contracts = _scan_contract_dir(_OMNIMARKET_NODES_DIR)
    assert contracts, f"No contracts found under {_OMNIMARKET_NODES_DIR}"

    gaps: list[tuple[str, str, Path]] = []

    for c in contracts:
        contract_path: Path = c.get("_path", Path("<unknown>"))
        contract_name: str = c.get("name") or contract_path.parent.name

        event_bus: dict[str, Any] = c.get("event_bus") or {}
        topics = _parse_topics(event_bus.get("subscribe_topics"))
        cmd_topics = [
            t
            for t in topics
            if t.startswith(_CMD_TOPIC_PREFIX) and t not in _ALLOWLISTED_TOPICS
        ]

        if not cmd_topics:
            continue  # No command subscriptions — nothing to check

        if _has_dispatcher_route(c):
            continue  # Has handler_routing or runtime_dispatch — covered

        # Gap: subscribed command topic(s) with no dispatcher route
        for topic in cmd_topics:
            gaps.append((topic, contract_name, contract_path))

    assert not gaps, (
        "omnimarket contracts have command topics subscribed without a dispatcher route:\n"
        + "\n".join(
            f"  {topic}  (contract: {name}, path: {path})" for topic, name, path in gaps
        )
        + "\n\nThis gate would have caught the June 9 DLQ regression and "
        "the June 12 DEL-01 live finding.\n"
        "Fix: add handler_routing or runtime_dispatch to each listed contract."
    )
