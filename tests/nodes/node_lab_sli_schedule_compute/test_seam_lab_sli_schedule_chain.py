# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the schedule contract's topics and models line up with the collect effect, and the in-memory bus delivers one collect request per boundary tick."""

from __future__ import annotations

import datetime as dt
import importlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.lab_sli.model_lab_sli_collect import (
    ModelLabSliCollectRequest,
)

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_SCHEDULE_CONTRACT = _NODES / "node_lab_sli_schedule_compute" / "contract.yaml"
_COLLECT_CONTRACT = _NODES / "node_lab_sli_collect_effect" / "contract.yaml"
_COMPUTE_CONTRACT = _NODES / "node_lab_sli_compute" / "contract.yaml"
_TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
_COLLECT_TOPIC = "onex.cmd.omnimarket.lab-sli-collect-requested.v1"


def _load(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle.read())


def test_schedule_publish_matches_collect_command_topic() -> None:
    schedule = _load(_SCHEDULE_CONTRACT)
    collect = _load(_COLLECT_CONTRACT)
    assert schedule["terminal_event"] == collect["runtime_dispatch"]["command_topic"]


def test_schedule_output_model_is_collect_input() -> None:
    schedule = _load(_SCHEDULE_CONTRACT)
    collect = _load(_COLLECT_CONTRACT)
    module = schedule["output_model"]["module"]
    cls = importlib.import_module(module).ModelLabSliCollectRequest
    assert cls is ModelLabSliCollectRequest
    assert collect["handler"]["input_model"].endswith(".ModelLabSliCollectRequest")


def test_schedule_subscribes_only_tick_topic() -> None:
    schedule = _load(_SCHEDULE_CONTRACT)
    assert schedule["event_bus"]["subscribe_topics"] == [_TICK_TOPIC]


def test_collect_is_wired_by_event_bus_to_its_command_and_success_topics() -> None:
    # A runtime wires a node only through event_bus.subscribe_topics; with runtime_dispatch alone the
    # collect node was skipped and the command topic had no consumer (OMN-24264).
    collect = _load(_COLLECT_CONTRACT)
    assert collect["event_bus"]["subscribe_topics"] == [
        collect["runtime_dispatch"]["command_topic"]
    ]
    assert (
        collect["runtime_dispatch"]["terminal_events"]["success"]
        in collect["event_bus"]["publish_topics"]
    )
    assert (
        collect["terminal_event"]
        == collect["runtime_dispatch"]["terminal_events"]["success"]
    )
    # The failure terminal is declared under runtime_dispatch, so a collect that refuses (pack off, unreadable
    # host) ends on a topic a reader can find, not on silence.
    failure = collect["runtime_dispatch"]["terminal_events"]["failure"]
    assert failure == "onex.evt.omnimarket.lab-sli-collect-failed.v1"


def test_collect_success_matches_compute_subscribe() -> None:
    collect = _load(_COLLECT_CONTRACT)
    compute = _load(_COMPUTE_CONTRACT)
    assert (
        collect["runtime_dispatch"]["terminal_events"]["success"]
        == compute["event_bus"]["subscribe_topics"][0]
    )


def _envelope_bytes(payload: object) -> bytes:
    envelope = ModelEventEnvelope[object](
        payload=payload, correlation_id=uuid4(), source_tool="lab-sli-schedule-bus-test"
    )
    return envelope.model_dump_json().encode("utf-8")


async def _wired_schedule(
    monkeypatch: pytest.MonkeyPatch, overlay_root: str
) -> tuple[EventBusInmemory, list[object]]:
    handler_cls = importlib.import_module(
        "omnimarket.nodes.node_lab_sli_schedule_compute.handlers.handler_lab_sli_schedule"
    ).HandlerLabSliSchedule
    handled: list[object] = []
    real_handle = handler_cls.handle

    def recording_handle(self: object, request: object) -> object:
        result = real_handle(self, request)
        handled.append(request)
        return result

    monkeypatch.setattr(handler_cls, "handle", recording_handle)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, overlay_root)
    manifest = discover_contracts_from_paths([_SCHEDULE_CONTRACT])
    assert manifest.total_errors == 0
    bus = EventBusInmemory(environment="test", group="lab-sli-schedule-bus")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="test"
    )
    assert report.total_wired == 1
    engine.freeze()
    return bus, handled


async def _published_payloads(
    bus: EventBusInmemory, topic: str
) -> list[dict[str, object]]:
    history = await bus.get_event_history(topic=topic)
    payloads: list[dict[str, object]] = []
    for message in history:
        payload = json.loads(message.value)["payload"]
        assert isinstance(payload, dict)
        payloads.append(payload)
    return payloads


@pytest.mark.asyncio
@pytest.mark.integration
async def test_boundary_tick_publishes_one_collect_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    overlay_root = tmp_path / "overlay"
    (overlay_root / "node_lab_sli_schedule_compute").mkdir(parents=True)
    (overlay_root / "node_lab_sli_schedule_compute" / "overlay.yaml").write_text(
        "schedule: {workflow: lab_sli, interval_seconds: 300, offset_seconds: 0}\n"
        "collector_host: host-a\n"
        "pr_watcher_state_path: /state/pr.json\n"
        'disk_paths: ["/"]\n',
        encoding="utf-8",
    )
    (overlay_root / "lab_sli_pack").mkdir()
    (overlay_root / "lab_sli_pack" / "overlay.yaml").write_text(
        "status: enabled\n", encoding="utf-8"
    )
    bus, handled = await _wired_schedule(monkeypatch, str(overlay_root))
    try:
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        await bus.publish(
            _TICK_TOPIC,
            None,
            _envelope_bytes(
                {
                    "now": now.isoformat(),
                    "scheduled_at": now.isoformat(),
                    "tick_interval_ms": 1000,
                    "tick_id": str(uuid4()),
                    "sequence_number": 1,
                    "correlation_id": str(uuid4()),
                    "scheduler_id": "test-scheduler",
                }
            ),
            None,
        )
        assert len(handled) == 1
        payloads = await _published_payloads(bus, _COLLECT_TOPIC)
        assert len(payloads) == 1
        ModelLabSliCollectRequest.model_validate(payloads[0])
    finally:
        await bus.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mid_window_tick_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    overlay_root = tmp_path / "overlay"
    (overlay_root / "node_lab_sli_schedule_compute").mkdir(parents=True)
    (overlay_root / "node_lab_sli_schedule_compute" / "overlay.yaml").write_text(
        "schedule: {workflow: lab_sli, interval_seconds: 300, offset_seconds: 0}\n"
        "collector_host: host-a\n"
        "pr_watcher_state_path: /state/pr.json\n"
        'disk_paths: ["/"]\n',
        encoding="utf-8",
    )
    (overlay_root / "lab_sli_pack").mkdir()
    (overlay_root / "lab_sli_pack" / "overlay.yaml").write_text(
        "status: enabled\n", encoding="utf-8"
    )
    bus, handled = await _wired_schedule(monkeypatch, str(overlay_root))
    try:
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=150)
        await bus.publish(
            _TICK_TOPIC,
            None,
            _envelope_bytes(
                {
                    "now": now.isoformat(),
                    "scheduled_at": now.isoformat(),
                    "tick_interval_ms": 1000,
                    "tick_id": str(uuid4()),
                    "sequence_number": 1,
                    "correlation_id": str(uuid4()),
                    "scheduler_id": "test-scheduler",
                }
            ),
            None,
        )
        # The tick reached the handler (so the empty topic is the schedule's decision, not a malformed tick).
        assert len(handled) == 1
        assert await _published_payloads(bus, _COLLECT_TOPIC) == []
    finally:
        await bus.close()
