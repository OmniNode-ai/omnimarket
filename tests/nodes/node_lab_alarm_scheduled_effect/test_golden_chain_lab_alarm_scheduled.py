# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the lab alarm's broker-lag run on the runtime schedule (OMN-24029)."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.lab_alarm_scheduled_tick import (
    ModelLabAlarmTickOutcome,
    run_lab_alarm_tick,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm_scheduled_effect.handlers.handler_lab_alarm_scheduled import (
    HandlerLabAlarmScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_lab_alarm_scheduled_effect"
ALARM_NODE = "node_lab_alarm"
ALARM_EXAMPLE = REPO / f"src/omnimarket/nodes/{ALARM_NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DONE = ModelLabAlarmTickOutcome(False, "outcome=ok declared=1")


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "lab_alarm",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "alarm_overlay_id": ALARM_NODE,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def alarm_environ(tmp_path: Path) -> dict[str, str]:
    """The lab alarm's own overlay, from the checked-in example."""
    (tmp_path / ALARM_NODE).mkdir()
    (tmp_path / ALARM_NODE / "overlay.yaml").write_text(
        ALARM_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _RecordingTick:
    """The injected tick: returns the fixed outcome and records the overlay id it was given."""

    def __init__(self, outcome: ModelLabAlarmTickOutcome) -> None:
        self._outcome = outcome
        self.calls: list[str] = []

    def __call__(
        self,
        *,
        alarm_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        build_handler: Callable[..., Any],
    ) -> ModelLabAlarmTickOutcome:
        self.calls.append(alarm_overlay_id)
        return self._outcome


def _raising(error: Exception) -> Any:
    def tick(
        *,
        alarm_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        build_handler: Callable[..., Any],
    ) -> ModelLabAlarmTickOutcome:
        raise error

    return tick


def test_golden_a_due_tick_runs_the_alarm_and_returns_its_detail(
    tmp_path: Path,
) -> None:
    recorded = _RecordingTick(DONE)
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=recorded)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "lab_alarm",
        NODE,
        "completed",
    )
    assert event.detail == DONE.detail
    assert recorded.calls == [ALARM_NODE]


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=_RecordingTick(DONE))
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None


def _add_restart_schedule(tmp_path: Path) -> dict[str, str]:
    env = environ(tmp_path)
    path = tmp_path / NODE / "overlay.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["container_restarts_probe"] = {
        "job_id": "lab_alarm_container_restarts",
        "run_interval_seconds": 60,
        "grace_seconds": 30,
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return env


def test_container_restarts_probe_uses_its_own_runtime_schedule(tmp_path: Path) -> None:
    env = _add_restart_schedule(tmp_path)
    lag = _RecordingTick(DONE)
    restarts = _RecordingTick(
        ModelLabAlarmTickOutcome(False, "container_restarts outcome=OK")
    )
    handler = HandlerLabAlarmScheduled(environ=env, tick=lag, restarts_tick=restarts)

    initial = handler.handle(tick(T0))
    assert isinstance(initial, ModelHandlerOutput)
    assert [event.job_id for event in initial.events] == [
        "lab_alarm",
        "lab_alarm_container_restarts",
    ]
    assert handler.handle(tick(T0 + timedelta(seconds=59))) is None
    restart_event = handler.handle(tick(T0 + timedelta(seconds=60)))
    assert isinstance(restart_event, ModelScheduleRunEvent)
    assert restart_event.job_id == "lab_alarm_container_restarts"
    assert restart_event.outcome == "completed"
    assert restart_event.detail == "container_restarts outcome=OK"
    assert handler.handle(tick(T0 + timedelta(seconds=61))) is None
    assert lag.calls == [ALARM_NODE]
    assert restarts.calls == [ALARM_NODE, ALARM_NODE]


@pytest.mark.asyncio
async def test_runtime_tick_publishes_lag_and_restart_probe_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _add_restart_schedule(tmp_path).items():
        monkeypatch.setenv(key, value)
    alarm_environ(tmp_path)
    alarm_path = tmp_path / ALARM_NODE / "overlay.yaml"
    alarm = yaml.safe_load(alarm_path.read_text(encoding="utf-8"))
    alarm["bus"]["backend"] = "runtime"
    manifest_path = tmp_path / "lane-manifest.yaml"
    manifest_path.write_text(
        "lanes: {dev: {services: [{name: runtime, restart_bound: 1}]}}\n",
        encoding="utf-8",
    )
    alarm["container_restarts"] = {
        "census_lane": "dev",
        "lane_manifest": str(manifest_path),
        "docker_command": [sys.executable, "-c", "print('1 running')"],
        "timeout_seconds": 5,
    }
    alarm_path.write_text(yaml.safe_dump(alarm), encoding="utf-8")

    # The probe's builder uses the runtime adapters and the fake Docker response.
    # Keep the broker-lag port on its fixture instead of connecting to a live broker.
    def build_lag(_: ModelLabAlarmOverlay) -> HandlerBrokerLag:
        fixture = ModelLabAlarmOverlay.model_validate(
            yaml.safe_load(ALARM_EXAMPLE.read_text(encoding="utf-8"))
        )
        from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import build_handler

        return build_handler(fixture)

    class FixtureScheduled(HandlerLabAlarmScheduled):
        def __init__(self) -> None:
            super().__init__(build_handler=build_lag)

    monkeypatch.setattr(
        f"omnimarket.nodes.{NODE}.handlers.handler_lab_alarm_scheduled.HandlerLabAlarmScheduled",
        FixtureScheduled,
    )
    contract = REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml"
    manifest = discover_contracts_from_paths([contract])
    assert manifest.total_errors == 0
    bus = EventBusInmemory(environment="test", group="lab-alarm-probe")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="test"
        )
        assert report.total_wired == 1
        engine.freeze()
        request = tick(T0)
        envelope = ModelEventEnvelope[ModelRuntimeTick](
            payload=request,
            correlation_id=request.correlation_id,
            source_tool="alarm-test",
        )
        await bus.publish(
            "onex.intent.platform.runtime-tick.v1",
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        messages = await bus.get_event_history(
            topic="onex.evt.omnimarket.schedule-run.v1"
        )
        events = [
            ModelScheduleRunEvent.model_validate(json.loads(message.value)["payload"])
            for message in messages
        ]
        assert [event.job_id for event in events] == [
            "lab_alarm",
            "lab_alarm_container_restarts",
        ]
        assert events[1].outcome == "completed"
        assert "container_restarts outcome=OK lane=dev readings=1" in events[1].detail
    finally:
        await bus.close()


def test_error_an_unreadable_alarm_overlay_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerLabAlarmScheduled(
        environ=environ(tmp_path),
        tick=_raising(NodeOverlayError("boom: the overlay is invalid")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the lab alarm overlay could not be read",
    )


def test_error_a_failed_run_outcome_carries_its_detail(tmp_path: Path) -> None:
    failed = ModelLabAlarmTickOutcome(
        True, "the lab alarm run failed: delivery-failed:down"
    )
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=_RecordingTick(failed))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == failed.detail


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerLabAlarmScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerLabAlarmScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("lab_alarm")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


_TOPICS = ("onex.evt.example.one.v1", "onex.evt.example.two.v1")


def _group(topic: str) -> str:
    return f"local.examplepkg.projection_example.consume.1.2.0.__i.runtime-main.__t.{topic}"


def _flat(value: int) -> dict[str, int]:
    return {_group(t): value for t in _TOPICS}


def _manifest() -> dict[str, object]:
    return {
        "contracts": [
            {
                "name": "projection_example",
                "package_name": "examplepkg",
                "contract_version": "1.2.0",
                "event_bus": {"subscribe_topics": list(_TOPICS)},
            }
        ]
    }


class _RaisingDelivery:
    """A delivery that cannot run: the handler names it in the run's failure."""

    def handle(self, request: object) -> object:
        raise OSError("the channel is down")


def _alarm_handler(
    lags: dict[str, int], previous: dict[str, int], delivery: Any = None
) -> HandlerBrokerLag:
    return HandlerBrokerLag(
        overlay=ModelLabAlarmOverlay.model_validate(
            yaml.safe_load(ALARM_EXAMPLE.read_text(encoding="utf-8"))
        ),
        manifest=InMemoryManifest(_manifest()),
        lag=InMemoryLag(lags),
        samples=InMemorySamples(previous),
        records=InMemoryRunRecords(),
        clock=lambda: T0,
        delivery=delivery,
    )


def _real(tmp_path: Path, handler: HandlerBrokerLag) -> ModelLabAlarmTickOutcome:
    return run_lab_alarm_tick(
        alarm_overlay_id=ALARM_NODE,
        environ=alarm_environ(tmp_path),
        clock=lambda: T0,
        handler=handler,
    )


def test_real_chain_a_flat_lag_reads_ok(tmp_path: Path) -> None:
    outcome = _real(tmp_path, _alarm_handler(_flat(7), _flat(7)))
    assert outcome == ModelLabAlarmTickOutcome(False, "outcome=ok declared=2")


def test_real_chain_an_unbound_lag_source_reads_failed_by_name(
    tmp_path: Path,
) -> None:
    outcome = run_lab_alarm_tick(
        alarm_overlay_id=ALARM_NODE,
        environ=alarm_environ(tmp_path),
        clock=lambda: T0,
    )
    assert outcome.failed is True
    assert "not bound" in outcome.detail


def test_real_chain_the_builder_builds_the_handler_from_the_overlay(
    tmp_path: Path,
) -> None:
    expected = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load(ALARM_EXAMPLE.read_text(encoding="utf-8"))
    )
    calls: list[ModelLabAlarmOverlay] = []

    def build(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
        calls.append(overlay)
        return _alarm_handler(_flat(7), _flat(7))

    outcome = run_lab_alarm_tick(
        alarm_overlay_id=ALARM_NODE,
        environ=alarm_environ(tmp_path),
        clock=lambda: T0,
        build_handler=build,
    )
    assert outcome == ModelLabAlarmTickOutcome(False, "outcome=ok declared=2")
    assert calls == [expected]


def test_real_chain_a_growing_lag_is_a_completed_run_that_names_the_alarm(
    tmp_path: Path,
) -> None:
    """Failing control: the same chain with one group grown by one reads alarm, not ok."""
    lags = _flat(7)
    lags[_group(_TOPICS[0])] = 8
    outcome = _real(tmp_path, _alarm_handler(lags, _flat(7)))
    assert outcome == ModelLabAlarmTickOutcome(
        False, "outcome=alarm alarms=1 delivered=0"
    )


def test_real_chain_no_previous_sample_reads_failed_by_name(tmp_path: Path) -> None:
    handler = _alarm_handler(_flat(7), {})
    vars(handler)["_samples"] = InMemorySamples(None)
    outcome = _real(tmp_path, handler)
    assert outcome.failed is True
    assert outcome.detail == "the lab alarm could not read the lag: no-previous-sample"


def test_real_chain_a_delivery_that_cannot_run_reads_failed(tmp_path: Path) -> None:
    lags = _flat(7)
    lags[_group(_TOPICS[0])] = 8
    outcome = _real(
        tmp_path, _alarm_handler(lags, _flat(7), delivery=_RaisingDelivery())
    )
    assert outcome.failed is True
    assert outcome.detail.startswith("the lab alarm run failed: ")
    assert "delivery-unavailable:OSError" in outcome.detail
