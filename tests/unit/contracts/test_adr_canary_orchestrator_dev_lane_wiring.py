# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The ADR canary chain must be reachable on the dev lane.

A command published to ``adr-canary-requested`` only reaches an orchestrator if
some runtime the dev lane starts owns that contract, and the orchestrator must
then wire at boot under strict mode with the bus the kernel registers. Every
stage command it publishes needs a subscriber owned by a dev-lane runtime too.
These tests load the real contracts from disk and wire them with the same
registrations the kernel makes; no handler is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.container import ModelONEXContainer
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from omnibase_core.protocols.event_bus.protocol_event_bus_subscriber import (
    ProtocolEventBusSubscriber,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    runtime_profile_owns_contract,
)
from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_ORCHESTRATOR = "node_adr_canary_orchestrator"
# The runtimes the dev lane compose starts that load Market nodes. Spelled here
# because the compose file lives in another repository; a node owned only by a
# profile outside this set has no process on the dev lane to subscribe it.
_DEV_LANE_PROFILES = ("main", "effects")


def _contract(node_name: str) -> dict[str, object]:
    loaded = yaml.safe_load((_NODES / node_name / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _owned_by_a_dev_lane_runtime(raw_contract: dict[str, object]) -> bool:
    return any(
        runtime_profile_owns_contract(raw_contract, profile, environ={})
        for profile in _DEV_LANE_PROFILES
    )


def _event_bus(raw_contract: dict[str, object]) -> dict[str, list[str]]:
    event_bus = raw_contract["event_bus"]
    assert isinstance(event_bus, dict)
    return event_bus


def test_a_dev_lane_runtime_owns_the_orchestrator() -> None:
    assert _owned_by_a_dev_lane_runtime(_contract(_ORCHESTRATOR))


def test_completed_event_is_the_declared_terminal_event() -> None:
    """`onex run-node` refuses a node that declares no terminal_event to poll."""
    contract = _contract(_ORCHESTRATOR)
    completed = "onex.evt.omnimarket.adr-canary-completed.v1"
    assert contract["terminal_event"] == completed
    assert completed in _event_bus(contract)["publish_topics"]


def test_the_canary_profile_still_owns_the_orchestrator() -> None:
    assert runtime_profile_owns_contract(_contract(_ORCHESTRATOR), "canary", environ={})


def test_every_stage_command_has_a_subscriber_owned_by_a_dev_lane_runtime() -> None:
    stage_commands = {
        topic
        for topic in _event_bus(_contract(_ORCHESTRATOR))["publish_topics"]
        if ".cmd." in topic
    }
    assert len(stage_commands) == 5, stage_commands

    unowned = set(stage_commands)
    for contract_path in sorted(_NODES.glob("*/contract.yaml")):
        raw = yaml.safe_load(contract_path.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get("event_bus"), dict):
            continue
        subscribed = set(raw["event_bus"].get("subscribe_topics") or [])
        if subscribed & stage_commands and _owned_by_a_dev_lane_runtime(raw):
            unowned -= subscribed
    assert not unowned, f"no dev-lane runtime subscribes: {sorted(unowned)}"


async def test_orchestrator_wires_from_the_real_contract_with_the_kernel_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero wiring failures against the registrations the kernel makes before wiring."""
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "local")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9")
    monkeypatch.delenv("ONEX_WIRING_STRICT_MODE", raising=False)
    discovered = discover_contracts_from_paths(
        [_NODES / _ORCHESTRATOR / "contract.yaml"]
    )
    assert not discovered.errors, discovered.errors

    bus = EventBusInmemory()
    container = ModelONEXContainer()
    await container.service_registry.register_instance(ProtocolEventBusPublisher, bus)
    await container.service_registry.register_instance(ProtocolEventBusSubscriber, bus)
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            MessageDispatchEngine(),
            event_bus=bus,
            environment="local",
            container=container,
            topology=load_topology_profile("local"),
        )
    finally:
        await bus.close()

    assert [result.outcome for result in report.results] == [EnumWiringOutcome.WIRED]
    assert not report.quarantined_handlers
