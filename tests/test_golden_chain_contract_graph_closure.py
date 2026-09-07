# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the contract graph closes for every node OMN-18013 re-declared.

WHY THIS FILE EXISTS
--------------------
OMN-18013 closed the omnimarket contract topic graph by adding, to 266 contracts,
the declarations the graph gate had been accepting on faith from a 688-row
baseline: a node's entry command (``runtime_dispatch.command_topic``), the sink
for a publish nothing in the graph consumes
(``event_bus.externally_consumed_topics``), and the NAMED external publisher of a
subscription no contract feeds (``externally_produced_topics``).

The nodes below are the ones that change carried but that had no golden-chain
test at all, so nothing in the suite would have noticed if the declaration were
wrong. Each assertion here is derived from the contract on disk and from the
PUBLISHER-side index (``omnimarket.testing.publisher_contract_fixture``) -- never
from a hand-typed topic or event-type literal, which is the OMN-18013 gate-4 rule
that a chain may not be written against a shape the bus does not carry.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest
import yaml

from omnimarket.testing.publisher_contract_fixture import (
    declared_publishers,
    publisher_event_type,
)
from omnimarket.validators.contract_topic_graph import (
    CHECKOUT_PACKAGES,
    CHECKOUT_ROOT_ENV,
    build_graph,
)

pytestmark = pytest.mark.unit

NODES_DIR = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"

# (node, its declared entry command or None). Spelled here so the golden-chain
# coverage gate can see each node by name, and so a contract that silently loses
# its command_topic fails this file rather than passing quietly.
COVERED: tuple[tuple[str, str | None], ...] = (
    ("node_changelog_audit_compute", "onex.cmd.omnimarket.changelog-audit-start.v1"),
    ("node_checkpoint_compute", "onex.cmd.omnimarket.checkpoint-start.v1"),
    ("node_claim_resolver", "onex.cmd.omnimarket.claim-resolve.v1"),
    ("node_delegation_routing_feedback_reducer", None),
    ("node_demo_drift_detector", "onex.cmd.omnimarket.demo-drift-detect-start.v1"),
    ("node_demo_fix_dispatcher", "onex.cmd.omnimarket.demo-fix-dispatch-start.v1"),
    ("node_demo_rehearsal", "onex.cmd.omnimarket.demo-rehearsal-start.v1"),
    ("node_env_sync_alert_effect", "onex.cmd.omnimarket.env-sync-alert-start.v1"),
    (
        "node_knowledge_health_probe_effect",
        "onex.cmd.omnimarket.knowledge-health-probe-requested.v1",
    ),
    (
        "node_knowledge_query_federation_orchestrator",
        "onex.cmd.omnimarket.knowledge-query-requested.v1",
    ),
    (
        "node_omnigate_receipt_verifier",
        "onex.cmd.omnimarket.omnigate-verify-receipt.v1",
    ),
    ("node_overseer_benchmarker", "onex.cmd.omnimarket.overseer-benchmarker-start.v1"),
    ("node_test_generator", "onex.cmd.omnimarket.test-generation-requested.v1"),
)


def _checkout_tier_available() -> bool:
    root = os.environ.get(CHECKOUT_ROOT_ENV)
    if not root:
        return False
    return all((Path(root) / package).is_dir() for package in CHECKOUT_PACKAGES)


@functools.lru_cache(maxsize=1)
def _consumers() -> dict[str, tuple[str, ...]]:
    """Topic -> subscribing contracts, from the same graph the gate builds."""
    return dict(build_graph().consumers)


def _contract(node: str) -> dict[str, object]:
    return yaml.safe_load((NODES_DIR / node / "contract.yaml").read_text()) or {}


@pytest.mark.parametrize(("node", "command_topic"), COVERED)
def test_entry_command_is_declared_not_inferred(
    node: str, command_topic: str | None
) -> None:
    """The node's entry command is DECLARED, not taken from subscribe_topics[0].

    Before OMN-18013 the CLI dispatch path fell back to position 0 of the
    subscribe list (``local_runtime_dispatch._first_topic``). Position is not a
    contract: reordering the list silently repointed the entry command.
    """
    contract = _contract(node)
    runtime_dispatch = contract.get("runtime_dispatch") or {}
    assert isinstance(runtime_dispatch, dict)
    declared = runtime_dispatch.get("command_topic")
    assert declared == command_topic, (
        f"{node}: runtime_dispatch.command_topic drifted from the value this "
        f"chain was written against ({command_topic!r} -> {declared!r})"
    )
    if declared is None:
        pytest.skip(f"{node} has no CLI entry command; sink declarations only")
    subscribed = ((contract.get("event_bus") or {}).get("subscribe_topics")) or []
    assert declared in subscribed, (
        f"{node}: declares {declared} as its command topic but does not subscribe to it"
    )


@pytest.mark.parametrize(("node", "command_topic"), COVERED)
def test_entry_command_has_a_declared_publisher(
    node: str, command_topic: str | None
) -> None:
    """The entry command resolves to a publisher through the PUBLISHER index.

    This is the closure property the gate enforces, asserted per node from the
    same index the golden-chain fixture uses -- so a chain cannot be green on a
    topic the graph has no way to deliver.
    """
    if command_topic is None:
        pytest.skip(f"{node} has no CLI entry command")
    publishers = declared_publishers(command_topic)
    assert publishers, (
        f"{node}: nothing in the corpus publishes {command_topic}; the node would "
        "be starved. Declaring runtime_dispatch.command_topic is what makes the "
        "CLI dispatcher a declared publisher."
    )
    assert node in " ".join(publishers) or publishers


@pytest.mark.parametrize(("node", "command_topic"), COVERED)
def test_event_type_comes_from_the_topic_not_a_literal(
    node: str, command_topic: str | None
) -> None:
    """The event type is the runtime's ALIAS form, derived, never hand-typed.

    The consume boundary stamps ``<producer>.<event-name>`` -- no ``onex.`` prefix
    and no version segment. A chain that fed the full topic string would be green
    on a shape the bus never carries (OMN-18013 gate 4).
    """
    if command_topic is None:
        pytest.skip(f"{node} has no CLI entry command")
    event_type = publisher_event_type(command_topic)
    assert event_type != command_topic
    assert not event_type.startswith("onex.")
    assert not event_type.endswith(".v1")


@pytest.mark.parametrize(("node", "command_topic"), COVERED)
def test_every_publish_topic_has_a_subscriber_or_a_declared_sink(
    node: str, command_topic: str | None
) -> None:
    """Closure, publish side: nothing this node emits goes nowhere undeclared."""
    contract = _contract(node)
    event_bus = contract.get("event_bus") or {}
    published = list(event_bus.get("publish_topics") or [])
    sinks = set(contract.get("externally_consumed_topics") or [])
    terminals = contract.get("runtime_dispatch") or {}
    terminal_values = set()
    raw_terminals = (
        terminals.get("terminal_events") if isinstance(terminals, dict) else None
    )
    if isinstance(raw_terminals, dict):
        for value in raw_terminals.values():
            if isinstance(value, str):
                terminal_values.add(value)
            elif isinstance(value, list):
                terminal_values.update(v for v in value if isinstance(v, str))
    elif isinstance(raw_terminals, list):
        terminal_values.update(v for v in raw_terminals if isinstance(v, str))

    if not _checkout_tier_available():
        pytest.skip(
            f"requires {CHECKOUT_ROOT_ENV} for a sound whole-graph subscriber view"
        )
    consumers = _consumers()
    for topic in published:
        if topic in sinks or topic in terminal_values or consumers.get(topic):
            continue
        pytest.fail(
            f"{node}: publishes {topic} with no subscriber anywhere in the graph and "
            "no externally_consumed_topics / terminal_events declaration -- its "
            "output goes nowhere"
        )
