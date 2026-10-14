# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One full morning cycle over the canonical in-memory bus, no injected gateway.

The start command is published on the contract's declared topic, the contract is
wired through the runtime's auto-wiring from its path, the handler builds its own
lab-work phase gateway, every phase is a lab work unit placed and sent by the
shared LabWorkCaller on the lab-work contract's topics and served by a real
LabWorkHost, and the terminal lands on the declared completed topic. Two
stand-ins: the host's shell runner, which answers each unit with a recorded
phase result in its output tail, and its capacity probe, which reads a fixed
idle host instead of this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring import (
    subscribe_wired_contract_topics,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.service_dispatch_result_applier import DispatchResultApplier
from pydantic import JsonValue

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.lab_work.bus import LabWorkHost
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    EnumLabWorkUnitStatus,
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers import (
    handler_morning_phase_lab_work,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    LABELS,
    OVERLAY_ENV,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
    ModelMorningGroundStateRequest,
    ModelMorningGroundStateResult,
)

from .helpers import NODE, OVERLAY_FILE, PHASES, request

PACKAGE = f"omnimarket.nodes.{NODE}"
SCHEMAS = cast(
    dict[str, dict[str, object]],
    json.loads(files(PACKAGE).joinpath("models/phase_schemas.json").read_text()),
)
PHASE_OF_LABEL = {
    "ledger-reconcile": "LedgerReconcile",
    "idempotency-precheck": "Precheck",
    "decisions-register": "DecisionsRegister",
    **{label: phase for phase, label in LABELS.items()},
}
LAB_WORK = {
    "repo": "OmniNode-ai/omnimarket",
    "commit_sha": "28219cf929b32bf51e0a83ac21d89caeed1a27d5",
    "lane": "morning-ground-state",
}


class IdleHost:
    """The capacity probe of a fixed idle pool host."""

    def handle(
        self, request: ModelHostCapacityProbeRequest
    ) -> ModelHostCapacityAdvertisement:
        return ModelHostCapacityAdvertisement(
            host_name=request.host_name,
            cores=16,
            load1=0.1,
            mem_available_bytes=64 * 1024**3,
            tools=request.tools,
            running_units=request.running_units,
            max_units=request.max_units,
            advertised_at=datetime.now(UTC),
            cadence_seconds=request.cadence_seconds,
        )


class RecordedAgent:
    """The pool host's runner: each unit's output tail is a recorded phase result."""

    def __init__(self, correlation: UUID, bad_phase: str | None) -> None:
        self.asked: list[str] = []
        self.bad_phase = bad_phase
        self.units: list[ModelLabWorkUnitRequest] = []
        self.label_of = {
            "mgs-" + uuid5(correlation, label).hex[:24]: label
            for label in PHASE_OF_LABEL
        }

    @property
    def host_name(self) -> str:
        return "pool-a"

    async def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.units.append(request)
        label = self.label_of[request.work_unit_id]
        phase = PHASE_OF_LABEL[label]
        self.asked.append(phase)
        result = phase_result(SCHEMAS.get(label))
        if phase == self.bad_phase and isinstance(result, dict):
            result["delegation"] = {"delegated": 1, "runs": [], "reason": ""}
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=self.host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            lane=request.lane,
            kind=request.kind,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=0,
            output_tail="agent session log\n" + json.dumps(result) + "\n",
        )


def minimal(schema: dict[str, object]) -> object:
    """The smallest instance of a phase result schema (required fields only)."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return cast(list[object], schema["enum"])[0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties = cast(dict[str, dict[str, object]], schema.get("properties", {}))
        required = cast(list[str], schema.get("required", []))
        return {name: minimal(properties[name]) for name in required}
    if kind == "array":
        return []
    if kind == "integer" or kind == "number":
        return 0
    if kind == "boolean":
        return False
    return "x"


def phase_result(schema: dict[str, object] | None) -> object:
    """A schema-valid recorded phase result carrying a valid delegation cell."""
    result = minimal(schema or {})
    properties = cast(dict[str, object], (schema or {}).get("properties", {}))
    if isinstance(result, dict) and "delegation" in properties:
        result["delegation"] = DELEGATION
    return result


DELEGATION = {"delegated": 1, "runs": ["fixture"], "reason": ""}


COMPLETED = "onex.evt.omnimarket.morning-ground-state-completed.v1"
FAILED = "onex.evt.omnimarket.morning-ground-state-failed.v1"
START = "onex.cmd.omnimarket.morning-ground-state-start.v1"


async def drive(
    bad_phase: str | None,
) -> tuple[list[ModelMorningGroundStateResult], list[bytes], list[str]]:
    """Publish one start command; return completed terminals, failed terminals, phases asked."""
    contract_file = files(PACKAGE).joinpath("contract.yaml")
    contract = yaml.safe_load(contract_file.read_text())
    start = contract["event_bus"]["subscribe_topics"][0]
    assert start == START
    bus = EventBusInmemory(environment="dev", group="morning-cycle")
    await bus.start()
    command = request(
        {"publish": False, "force": True, "labWork": cast(JsonValue, LAB_WORK)}
    )
    agent = RecordedAgent(command.correlation_id, bad_phase)
    host = LabWorkHost(cast(ProtocolLabRunBus, bus), agent, IdleHost(), max_units=4)
    await host.start(advertise=False)

    async def advertise() -> None:
        while True:
            await host.advertise_once()
            await asyncio.sleep(0.05)

    beat = asyncio.create_task(advertise())
    completed: list[ModelMorningGroundStateResult] = []
    failed: list[bytes] = []
    settled = asyncio.Event()

    async def on_completed(message: ModelEventMessage) -> None:
        completed.append(
            ModelEventEnvelope[ModelMorningGroundStateResult]
            .model_validate_json(message.value)
            .payload
        )
        settled.set()

    async def on_failed(message: ModelEventMessage) -> None:
        failed.append(message.value)
        settled.set()

    stops = [
        await bus.subscribe(
            COMPLETED, group_id="morning-cycle-ok", on_message=on_completed
        ),
        await bus.subscribe(FAILED, group_id="morning-cycle-err", on_message=on_failed),
    ]
    manifest = discover_contracts_from_paths([Path(str(contract_file))])
    assert manifest.total_discovered == 1
    assert not manifest.errors
    engine = MessageDispatchEngine()
    appliers = {
        manifest.contracts[0].name: DispatchResultApplier(
            cast(ProtocolEventBusLike, bus),
            output_topic=contract["terminal_event"],
            allowed_output_topics=contract["event_bus"]["publish_topics"],
        )
    }
    report = await wire_from_manifest(
        manifest,
        engine,
        bus,
        subscribe_immediately=False,
        result_appliers_by_contract=appliers,
        materialized_explicit_dependencies={
            "HandlerMorningGroundState": {"event_bus": bus}
        },
    )
    engine.freeze()
    attached = await subscribe_wired_contract_topics(
        manifest, report, engine, bus, result_appliers_by_contract=appliers
    )
    assert start in attached[manifest.contracts[0].name]
    envelope = ModelEventEnvelope[ModelMorningGroundStateRequest](
        payload=command,
        correlation_id=command.correlation_id,
        tenant_id=command.tenant_id,
        event_type=start,
        envelope_timestamp=command.emitted_at,
    )
    await bus.publish(
        start, str(command.correlation_id).encode(), envelope.model_dump_json().encode()
    )
    await asyncio.wait_for(settled.wait(), timeout=60)
    beat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await beat
    await host.stop()
    for stop in stops:
        await stop()
    await bus.close()
    assert all(unit.need_tools == ["claude", "claude-login"] for unit in agent.units)
    assert all(unit.argv[0] == "python3" for unit in agent.units)
    return completed, failed, agent.asked


@pytest.fixture
def bus_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    monkeypatch.setenv("ONEX_ENVIRONMENT", "dev")
    monkeypatch.setattr(
        handler_morning_phase_lab_work, "ADVERTISEMENT_WINDOW_SECONDS", 0.2
    )


@pytest.mark.usefixtures("bus_environment")
def test_one_morning_cycle_over_the_bus() -> None:
    completed, failed, asked = asyncio.run(drive(None))
    assert failed == []
    assert len(completed) == 1
    terminal = completed[0]
    assert terminal.correlation_id == UUID("12345678-1234-5678-1234-567812345678")
    assert terminal.phases_run == ["GroundState", "Triage", "Reconcile", "Integrate"]
    assert terminal.expensive_agents_spawned == 4
    assert terminal.unconditional_agents_spawned == 2
    # Ledger reconcile, decisions register, then the six phases: each a lab work unit.
    assert asked[:2] == ["LedgerReconcile", "DecisionsRegister"]
    assert asked[-1] == "Goal"
    assert set(PHASES) <= set(asked)


@pytest.mark.usefixtures("bus_environment")
@pytest.mark.parametrize("bad_phase", ["GroundState", "Reconcile", "DroppedWork"])
def test_invalid_delegation_evidence_over_the_bus_terminates_as_failed(
    bad_phase: str,
) -> None:
    completed, failed, asked = asyncio.run(drive(bad_phase))
    assert completed == []
    assert len(failed) == 1
    assert b"delegation cell refused" in failed[0]
    assert "Goal" not in asked
