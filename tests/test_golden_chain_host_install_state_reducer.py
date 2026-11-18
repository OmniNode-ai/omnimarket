# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a host install step event on the bus ends as one state-updated record on the bus (OMN-25040).

Run on an in-memory bus. The reducer node is booted through ``wire_from_manifest`` from its real contract, a step event is
published on the contract's subscribe topic as the orchestrators publish it, and the record is read back from the
state-updated topic. Before the contract declared the record as a published event, the runtime kept the folded record as a
projection and published nothing there, so the branch deploy orchestrator and the host install orchestrator stopped after
their first step on the dev runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
)
from tests.nodes.node_host_install_state_reducer.support import (
    HOST,
    NODE_DIR,
    RUN_ID,
    UNIT,
    request,
)

pytestmark = pytest.mark.unit

STEP_TOPIC = "onex.evt.omnimarket.host-install-step.v1"
STATE_TOPIC = "onex.evt.omnimarket.host-install-state-updated.v1"


async def test_a_request_step_ends_as_one_requested_record_on_the_state_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_dir = tmp_path / NODE_DIR.name
    overlay_dir.mkdir()
    shutil.copy(NODE_DIR / "overlay.example.yaml", overlay_dir / "overlay.yaml")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))

    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="host-install-state", group="host-install-state")
    await bus.start()
    records: list[ModelHostInstallRecord] = []
    done = asyncio.Event()

    async def on_state(message: object) -> None:
        body = json.loads(bytes(getattr(message, "value", b"")))
        records.append(ModelHostInstallRecord.model_validate(body["payload"]))
        done.set()

    await bus.subscribe(STATE_TOPIC, on_message=on_state, group_id="probe")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()
    command = ModelHostInstallReduceCommand(prior=None, event=request())
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=command.model_dump(mode="json"), correlation_id=RUN_ID
    )
    await bus.publish(
        STEP_TOPIC, None, envelope.model_dump_json().encode("utf-8"), None
    )
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(done.wait(), timeout=30)
    await bus.close()

    (record,) = records
    assert (record.host, record.unit, record.correlation_id) == (HOST, UNIT, RUN_ID)
    assert record.state is EnumHostInstallState.REQUESTED
