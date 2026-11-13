# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the whole lab SLI chain (schedule -> collect -> compute) on the in-memory bus."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.lab_sli import (
    ModelLabSliEvaluationBatch,
    ModelLabSliReadingBatch,
)

_NODE = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_SCHEDULE_CONTRACT = _NODE / "node_lab_sli_schedule_compute" / "contract.yaml"
_COLLECT_CONTRACT = _NODE / "node_lab_sli_collect_effect" / "contract.yaml"
_COMPUTE_CONTRACT = _NODE / "node_lab_sli_compute" / "contract.yaml"

_TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
_COLLECT_TOPIC = "onex.cmd.omnimarket.lab-sli-collect-requested.v1"
_EVALUATE_TOPIC = "onex.cmd.omnimarket.lab-sli-evaluate-requested.v1"
_EVALUATED_TOPIC = "onex.evt.omnimarket.lab-sli-evaluated.v1"


def _envelope_bytes(payload: object) -> bytes:
    envelope = ModelEventEnvelope[object](
        payload=payload, correlation_id=uuid4(), source_tool="lab-sli-chain-bus-test"
    )
    return envelope.model_dump_json().encode("utf-8")


async def _wired_chain(
    monkeypatch: pytest.MonkeyPatch,
    overlay_root: str,
    contracts: list[Path],
) -> EventBusInmemory:
    manifest = discover_contracts_from_paths(contracts)
    assert manifest.total_errors == 0
    bus = EventBusInmemory(environment="test", group="lab-sli-chain-bus")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="test"
    )
    assert report.total_wired == len(contracts)
    engine.freeze()
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, overlay_root)
    return bus


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


def _tick_envelope(now: dt.datetime) -> bytes:
    return _envelope_bytes(
        {
            "now": now.isoformat(),
            "scheduled_at": now.isoformat(),
            "tick_interval_ms": 1000,
            "tick_id": str(uuid4()),
            "sequence_number": 1,
            "correlation_id": str(uuid4()),
            "scheduler_id": "test-scheduler",
        }
    )


def _make_overlay_root(tmp_path: Path) -> Path:
    overlay_root = tmp_path / "overlay"
    (overlay_root / "node_lab_sli_schedule_compute").mkdir(parents=True)
    (overlay_root / "node_lab_sli_schedule_compute" / "overlay.yaml").write_text(
        "schedule: {workflow: lab_sli, interval_seconds: 300, offset_seconds: 0}\n"
        "collector_host: chain-host\n"
        'pr_watcher_state_path: ""\n'
        "disk_paths: []\n",
        encoding="utf-8",
    )
    (overlay_root / "lab_sli_pack").mkdir()
    (overlay_root / "lab_sli_pack" / "overlay.yaml").write_text(
        "status: enabled\n", encoding="utf-8"
    )
    return overlay_root


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tick_on_a_window_boundary_reaches_the_evaluated_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_root = _make_overlay_root(tmp_path)
    bus = await _wired_chain(
        monkeypatch,
        str(overlay_root),
        [_SCHEDULE_CONTRACT, _COLLECT_CONTRACT, _COMPUTE_CONTRACT],
    )
    try:
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        await bus.publish(_TICK_TOPIC, None, _tick_envelope(now), None)

        collect_payloads = await _published_payloads(bus, _COLLECT_TOPIC)
        evaluate_payloads = await _published_payloads(bus, _EVALUATE_TOPIC)
        evaluated_payloads = await _published_payloads(bus, _EVALUATED_TOPIC)

        assert len(collect_payloads) == 1, (
            f"collect topic {_COLLECT_TOPIC} stayed empty"
        )
        assert len(evaluate_payloads) == 1, (
            f"evaluate topic {_EVALUATE_TOPIC} stayed empty"
        )
        assert len(evaluated_payloads) == 1, (
            f"evaluated topic {_EVALUATED_TOPIC} stayed empty"
        )

        ModelLabSliReadingBatch.model_validate(evaluate_payloads[0])
        ModelLabSliEvaluationBatch.model_validate(evaluated_payloads[0])
    finally:
        await bus.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_failing_control_a_mid_window_tick_stops_at_the_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_root = _make_overlay_root(tmp_path)
    bus = await _wired_chain(
        monkeypatch,
        str(overlay_root),
        [_SCHEDULE_CONTRACT, _COLLECT_CONTRACT, _COMPUTE_CONTRACT],
    )
    try:
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=150)
        await bus.publish(_TICK_TOPIC, None, _tick_envelope(now), None)

        collect_payloads = await _published_payloads(bus, _COLLECT_TOPIC)
        evaluate_payloads = await _published_payloads(bus, _EVALUATE_TOPIC)
        evaluated_payloads = await _published_payloads(bus, _EVALUATED_TOPIC)

        assert collect_payloads == [], f"collect topic {_COLLECT_TOPIC} should be empty"
        assert evaluate_payloads == [], (
            f"evaluate topic {_EVALUATE_TOPIC} should be empty"
        )
        assert evaluated_payloads == [], (
            f"evaluated topic {_EVALUATED_TOPIC} should be empty"
        )
    finally:
        await bus.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_failing_control_without_the_collect_event_bus_the_chain_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_root = _make_overlay_root(tmp_path)
    bus = await _wired_chain(
        monkeypatch, str(overlay_root), [_SCHEDULE_CONTRACT, _COMPUTE_CONTRACT]
    )
    try:
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        await bus.publish(_TICK_TOPIC, None, _tick_envelope(now), None)

        collect_payloads = await _published_payloads(bus, _COLLECT_TOPIC)
        evaluate_payloads = await _published_payloads(bus, _EVALUATE_TOPIC)
        evaluated_payloads = await _published_payloads(bus, _EVALUATED_TOPIC)

        assert len(collect_payloads) == 1, (
            f"collect topic {_COLLECT_TOPIC} should have one message"
        )
        assert evaluate_payloads == [], (
            f"evaluate topic {_EVALUATE_TOPIC} should be empty"
        )
        assert evaluated_payloads == [], (
            f"evaluated topic {_EVALUATED_TOPIC} should be empty"
        )
    finally:
        await bus.close()
