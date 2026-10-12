# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merge sweep requested over the bus, as a caller requests it (OMN-20676).

The caller's entry is ``onex node node_merge_sweep_orchestrator --input <request.json>``: the
canonical local runtime loads the packaged contract, publishes the request on the contract's
command topic, the handler the contract routes answers it, and the result lands on the declared
completed topic. These tests run that runtime over the recorded sweep with dispatch off, so no
lane is started.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_local import RuntimeLocal
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    HandlerMergeSweepRun,
)
from omnimarket.nodes.node_merge_sweep_orchestrator.models import (
    ModelMergeSweepRunRequest,
    ModelMergeSweepRunResult,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import load_recorded
from .support import run_request

NAME = "node_merge_sweep_orchestrator"
CONTRACT = Path(str(files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml")))
RECORDED = load_recorded()


def _over_the_bus(
    request: ModelMergeSweepRunRequest, tmp_path: Path
) -> tuple[EnumWorkflowResult, ModelMergeSweepRunResult]:
    """Run the packaged contract on the local runtime with the request as its input."""
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=60,
    )
    outcome = runtime.run()
    state = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert state["terminal_payload"] is not None, state
    return outcome, ModelMergeSweepRunResult.model_validate(state["terminal_payload"])


def test_a_sweep_requested_on_the_command_topic_completes_with_its_plan(
    tmp_path: Path,
) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path / "files", dispatch=False)
    outcome, result = _over_the_bus(request, tmp_path)
    assert outcome == EnumWorkflowResult.COMPLETED
    assert result.ok, result.why
    assert result.outcomes == []
    assert result.plan is not None
    assert result.plan.lanes
    assert result.plan == HandlerMergeSweepRun().handle(request).plan


def test_a_refused_sweep_answers_on_the_bus_and_starts_no_lane(
    tmp_path: Path,
) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path / "files", dispatch=False)
    Path(request.state_path).unlink()
    _, result = _over_the_bus(request, tmp_path)
    assert result.ok is False
    assert result.why
    assert result.plan is None
    assert result.outcomes == []


@pytest.mark.parametrize("missing_state", [False, True])
async def test_scheduled_ticks_request_and_complete_one_sweep_per_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_state: bool
) -> None:
    """Boot the three packaged contracts, including the request producer and real sweep handler."""
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path / "files", dispatch=False)
    if missing_state:
        Path(request.state_path).unlink()
    for field in (
        "state_path",
        "ledger_path",
        "floors_path",
        "runner_script",
        "brief_dir",
        "lane",
        "parent",
        "ticket",
        "model",
    ):
        monkeypatch.setenv(f"MERGE_SWEEP_{field.upper()}", str(getattr(request, field)))
    monkeypatch.setenv("MERGE_SWEEP_DISPATCH", "false")
    # The opening tick and its successor, twice. Only the opening ticks may request a run.
    now = dt.datetime.fromisoformat(request.now.replace("Z", "+00:00"))
    start = now - dt.timedelta(
        seconds=int(now.timestamp()) % 300, microseconds=now.microsecond
    )
    plan_contract = (
        CONTRACT.parent.parent / "node_merge_sweep_plan_compute" / "contract.yaml"
    )
    plan_raw = yaml.safe_load(plan_contract.read_text())
    run_raw = yaml.safe_load(CONTRACT.read_text())
    tick_topic = plan_raw["input_subscriptions"][0]["topic"]
    command_topic = run_raw["runtime_dispatch"]["command_topic"]
    completed_topic = run_raw["terminal_event"]
    planned_topic = plan_raw["terminal_event"]
    seen: dict[str, list[dict[str, object]]] = {
        planned_topic: [],
        command_topic: [],
        completed_topic: [],
    }
    bus = EventBusInmemory(environment="sweep-cycle", group="sweep-cycle")
    await bus.start()
    try:
        for topic in seen:

            async def collect(message: object, destination: str = topic) -> None:
                seen[destination].append(json.loads(bytes(message.value)))

            await bus.subscribe(topic, on_message=collect, group_id="probe")
        request_contract = (
            CONTRACT.parent.parent
            / "node_merge_sweep_request_compute"
            / "contract.yaml"
        )
        request_raw = yaml.safe_load(request_contract.read_text())
        assert request_raw["terminal_event"] == command_topic
        assert command_topic == "onex.cmd.omnimarket.merge-sweep-run-requested.v1"
        manifest = discover_contracts_from_paths(
            [plan_contract, request_contract, CONTRACT]
        )
        assert not manifest.errors, manifest.errors
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_failed == 0, report
        assert report.total_wired == 3, report
        engine.freeze()
        expected_correlations = set()
        for seconds in (0, 1, 300, 301):
            stamp = start + dt.timedelta(seconds=seconds)
            tick = ModelRuntimeTick(
                now=stamp,
                scheduled_at=stamp,
                tick_id=uuid4(),
                sequence_number=seconds + 1,
                correlation_id=uuid4(),
                scheduler_id="sweep-cycle",
                tick_interval_ms=1000,
            )
            if seconds in (0, 300):
                expected_correlations.add(str(tick.correlation_id))
            envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
                payload=tick.model_dump(mode="json"),
                correlation_id=tick.correlation_id,
                envelope_timestamp=stamp,
                event_type="platform.runtime-tick",
            )
            await bus.publish(
                tick_topic, None, envelope.model_dump_json().encode(), None
            )
        await asyncio.sleep(2)
        assert len(seen[planned_topic]) == 2, (
            seen
        )  # Positive control: the tick route fired.
        assert len(seen[command_topic]) == 2, seen
        assert len(seen[completed_topic]) == 2, seen
        for topic in seen:
            assert {e["correlation_id"] for e in seen[topic]} == expected_correlations
        for event in seen[command_topic]:
            generated = ModelMergeSweepRunRequest.model_validate(event["payload"])
            assert generated.state_path == request.state_path
            assert generated.dispatch is False
            assert generated.now in {
                start.isoformat().replace("+00:00", "Z"),
                (start + dt.timedelta(seconds=300)).isoformat().replace("+00:00", "Z"),
            }
        for event in seen[completed_topic]:
            result = ModelMergeSweepRunResult.model_validate(event["payload"])
            assert result.outcomes == []
            if missing_state:
                assert result.ok is False
                assert result.why == f"no state file at {request.state_path}"
                assert result.plan is None
            else:
                assert result.ok, result.why
                assert result.plan is not None
    finally:
        await bus.close()


def test_scheduled_request_uses_only_supplied_template_and_matching_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire
    from omnimarket.nodes.node_merge_sweep_request_compute.handlers import (
        handler_merge_sweep_scheduled_request as module,
    )

    template = run_request(
        RECORDED["scenarios"][0], RECORDED["base"], tmp_path / "files", dispatch=False
    )
    original = template.model_dump()

    def refuse_config_read() -> None:
        raise AssertionError("handle must not read deployment configuration")

    monkeypatch.setattr(module, "scheduled_request_config", refuse_config_read)
    handler = module.HandlerMergeSweepScheduledRequest(
        template=template, workflow="merge-sweep"
    )
    now = dt.datetime(2026, 10, 10, 6, tzinfo=dt.UTC)
    fire = ModelScheduledFire(
        workflow="merge-sweep",
        fire_id="merge-sweep-20261010T060000Z",
        window_start=now,
        interval_seconds=300,
        tick_id=uuid4(),
        tick_now=now,
        scheduler_id="sweep-cycle",
    )
    assert (
        handler.handle(fire.model_copy(update={"workflow": "other-workflow"})) is None
    )
    generated = handler.handle(fire)
    assert generated is not None
    assert handler.handle(fire) == generated
    assert template.model_dump() == original
    assert generated.state_path == template.state_path
    assert generated.parent == template.parent
    assert generated.dispatch is False
