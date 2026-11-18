# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A node's declared command topic is one of its subscribe topics, so the runtime attaches it (OMN-25454).

Four operation-routed nodes declared ``runtime_dispatch.command_topic`` and an empty
``event_bus.subscribe_topics``: node_floor_simple_compute, node_daily_line_compute,
node_pr_landing_ledger_facts_compute and node_bus_login_exposure_effect.
The runtime attaches a contract only through its subscribe topics, so a demand published on
the command topic reached nobody. Each case loads the real contract from disk, reads it back
the way the runtime does (``discover_contracts_from_paths``), boots it through
``wire_from_manifest`` on an in-memory bus and asserts the command topic is subscribed, the
runtime profile is declared, the terminal topics are published and the broker grants cover
the runtime's read of the command topic and write of every published topic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
GROUP = [
    "node_floor_simple_compute",
    "node_daily_line_compute",
    "node_pr_landing_ledger_facts_compute",
    "node_bus_login_exposure_effect",
]


def _contract(name: str) -> dict[str, Any]:
    return yaml.safe_load((NODES / name / "contract.yaml").read_text(encoding="utf-8"))


def _terminal_topics(contract: dict[str, Any]) -> list[str]:
    dispatch = contract["runtime_dispatch"]
    topics: list[str] = []
    events = dispatch.get("terminal_events") or {}
    # The success terminal must be a declared publish topic. A failure terminal is
    # published by the runtime's failure-terminal guard, which fires only for a result
    # carrying terminal_failure_cause; a node whose result has no such field leaves it
    # out of publish_topics (contract publish parity, OMN-17017).
    if "success" in events:
        topics.append(events["success"])
    single = contract.get("terminal_event")
    if isinstance(single, str):
        topics.append(single)
    return topics


@pytest.mark.parametrize("name", GROUP)
def test_command_topic_is_subscribed_and_node_has_a_profile(name: str) -> None:
    contract = _contract(name)
    command = contract["runtime_dispatch"]["command_topic"]
    bus = contract["event_bus"]
    assert command in bus["subscribe_topics"], (
        f"{name}: command topic {command} is not in subscribe_topics, so the runtime never attaches it"
    )
    profiles = contract.get("runtime_profiles") or contract["descriptor"].get(
        "runtime_profiles"
    )
    assert profiles, f"{name}: no runtime profile"
    terminals = _terminal_topics(contract)
    assert terminals, f"{name}: no terminal event declared"
    for topic in terminals:
        assert topic in bus["publish_topics"], f"{name}: {topic} not published"


@pytest.mark.parametrize("name", GROUP)
def test_broker_grants_cover_the_command_and_every_published_topic(name: str) -> None:
    contract = _contract(name)
    command = contract["runtime_dispatch"]["command_topic"]
    published = contract["event_bus"]["publish_topics"]
    grants = contract.get("broker_acl_grants") or []
    assert grants, f"{name}: no broker_acl_grants"
    pairs = {(g["name"], g["operation"]) for entry in grants for g in entry["grants"]}
    assert (command, "READ") in pairs
    for topic in published:
        assert (topic, "WRITE") in pairs, f"{name}: no WRITE grant for {topic}"


@pytest.mark.parametrize("name", GROUP)
async def test_runtime_discovers_the_subscription_and_wires_the_node(name: str) -> None:
    path = NODES / name / "contract.yaml"
    command = _contract(name)["runtime_dispatch"]["command_topic"]
    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors, manifest.errors
    (discovered,) = manifest.contracts
    assert discovered.event_bus is not None, f"{name}: runtime reads no event_bus"
    assert command in discovered.event_bus.subscribe_topics

    engine = MessageDispatchEngine()
    bus = EventBusInmemory(environment="gap-b", group="gap-b")
    await bus.start()
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
    finally:
        await bus.close()
    assert report.total_failed == 0
    assert report.total_wired == 1


@pytest.mark.parametrize(
    ("name", "command", "published"),
    [
        (
            "node_floor_simple_compute",
            "onex.cmd.omnimarket.floor-simple-compute-requested.v1",
            ["onex.evt.omnimarket.floor-simple-computed.v1"],
        ),
        (
            "node_daily_line_compute",
            "onex.cmd.omnimarket.daily-line-compute-requested.v1",
            ["onex.evt.omnimarket.daily-line-computed.v1"],
        ),
        (
            "node_pr_landing_ledger_facts_compute",
            "onex.cmd.omnimarket.pr-landing-ledger-facts-requested.v1",
            [
                "onex.evt.omnimarket.pr-landing-ledger-facts-completed.v1",
            ],
        ),
        (
            "node_bus_login_exposure_effect",
            "onex.cmd.omnimarket.bus-login-exposure-probe-requested.v1",
            [
                "onex.evt.omnimarket.bus-login-exposure-probed.v1",
                "onex.evt.omnimarket.bus-login-exposure-probe-failed.v1",
            ],
        ),
    ],
)
def test_declared_topics_are_the_expected_ones(
    name: str, command: str, published: list[str]
) -> None:
    contract = _contract(name)
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == published
