# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host nodes use the runtime's real contract discovery, dispatch and result publisher."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.handlers.handler_host_profile import parse_host_profile
from omnimarket.models.model_host_profile import ModelHostProfile

NODES = (
    "node_host_state_effect",
    "node_host_profile_compute",
    "node_host_drift_check_effect",
    "node_host_gap_unit_effect",
)
NODE_ROOT = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
# Public output API asserted against the terminal topic actually wired and observed below.
EXPECTED_TERMINALS = {
    "node_host_state_effect": "onex.evt.omnimarket.host-state-completed.v1",
    "node_host_profile_compute": "onex.evt.omnimarket.host-profile-completed.v1",
    "node_host_drift_check_effect": "onex.evt.omnimarket.host-drift-check-completed.v1",
    "node_host_gap_unit_effect": "onex.evt.omnimarket.host-gap-unit-completed.v1",
}


def local_profile(tmp_path: Path, source: str) -> ModelHostProfile:
    registry = tmp_path / "registry"
    clone = registry / "sample"
    (clone / ".git").mkdir(parents=True)
    values = {
        "registry_root": str(registry),
        "python": "3.12",
        "git_remote_template": "https://example.invalid/{repo}.git",
        "clones": [{"repo": "Example/sample", "ref": "dev", "path": str(clone)}],
    }
    if source == "inmemory":
        return ModelHostProfile.model_validate({"host": "sample", **values})
    overlay = tmp_path / "host_profiles.yaml"
    overlay.write_text(yaml.safe_dump({"hosts": {"sample": values}}))
    return parse_host_profile(overlay.read_text(), "sample")


async def exercise(
    node: str, source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bus: Any
) -> None:
    # A local process adapter with an empty systemd inventory, scoped to this test's temp directory.
    commands = tmp_path / "bin"
    commands.mkdir()
    systemctl = commands / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{commands}:{os.environ['PATH']}")
    profile = local_profile(tmp_path, source)
    contract_path = NODE_ROOT / node / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text())
    command = contract["runtime_dispatch"]["command_topic"]
    terminal = contract["terminal_event"]
    assert terminal == EXPECTED_TERMINALS[node]
    discovery = discover_contracts_from_paths([contract_path])
    assert not discovery.errors, discovery.errors
    assert len(discovery.contracts) == 1
    received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def collect(message: Any) -> None:
        await received.put(json.loads(message.value))

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(discovery.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_wired == 1, report
    assert report.total_failed == 0, report
    assert not report.quarantined_handlers, report
    assert report.results[0].topics_subscribed == (command,)
    engine.freeze()
    await bus.subscribe(terminal, group_id=f"host-test-{uuid4()}", on_message=collect)
    payload: dict[str, Any] = {"profile": profile.model_dump(mode="json")}
    if node == "node_host_gap_unit_effect":
        payload["unit"] = "g1_clones"
    correlation = uuid4()
    envelope = ModelEventEnvelope[object](
        payload=payload,
        correlation_id=correlation,
        event_type=contract["input_model"]["name"],
    )
    await bus.publish(command, None, envelope.model_dump_json().encode(), None)
    async with asyncio.timeout(30):
        while True:
            event = await received.get()
            if event.get("correlation_id") == str(correlation):
                break
    answer = event["payload"]
    assert answer["host"] == profile.host, event
    if node == "node_host_state_effect":
        assert answer["errors"] == []
        assert answer["present_clone_paths"] == [profile.clones[0].path]
    elif node == "node_host_profile_compute":
        assert answer["clones"][0]["path"] == profile.clones[0].path
    elif node == "node_host_drift_check_effect":
        assert answer["ok"] is True, event
        assert answer["findings"] == []
    else:
        assert answer["applied"] is False
        assert answer["planned"] == []
        assert answer["done"] == []


@pytest.mark.parametrize("node", NODES)
@pytest.mark.parametrize("source", ["inmemory", "overlay"])
async def test_host_declaration_runtime_inmemory_two_profiles(
    node: str, source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        await exercise(node, source, tmp_path, monkeypatch, bus)
    finally:
        await bus.close()
