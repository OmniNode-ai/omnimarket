# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the on-demand route of the host hygiene orchestrator (OMN-24610).

The real contract.yaml is wired through ``wire_from_manifest`` onto an in-memory bus. A host disk reading arrives on the watermark node's read topic in the wire shape, then an on-demand request is published on the contract's first subscribed topic exactly the way ``onex run-node`` builds it; the run event leaves on the contract's terminal topic carrying the caller's correlation id. The overlay is the node's in-memory profile, found through ONEX_SKILL_OVERLAY_ROOTS.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.cli.cli_run_node import _normalize_payload, _resolve_node_topics
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.model_host_disk_reading import (
    ModelDiskClassReading,
    ModelHostDiskRead,
    ModelHostDiskVolume,
)
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers import (
    HOST_DISK_READINGS,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers import (
    handler_host_hygiene_run_requested as run_requested_module,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)

pytestmark = pytest.mark.unit

NODE = "node_host_hygiene_orchestrator"
NODE_DIR = (
    Path(__file__).resolve().parents[2].parent / "src" / "omnimarket" / "nodes" / NODE
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
ROUTES = {r["operation"]: r["topic"] for r in CONTRACT["handler_routing"]["handlers"]}
READ_TOPIC = ROUTES["record_host_disk_read"]
# The topic `onex run-node` publishes on is read through the client's own resolver, and must be the route's topic.
REQUEST_TOPIC = _resolve_node_topics(NODE)[1]
assert ROUTES["host_hygiene.run_requested"] == REQUEST_TOPIC
assert CONTRACT["event_bus"]["subscribe_topics"][0] == REQUEST_TOPIC
RUN_TOPIC, SCHEDULE_TOPIC = CONTRACT["event_bus"]["publish_topics"]
OVERLAY = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
HOST = OVERLAY["hosts"][0]["name"]
VOLUME = OVERLAY["hosts"][0]["pressure_volume"]
NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.UTC)


def _wire(event: object, event_type: str) -> bytes:
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=event, correlation_id=uuid.uuid4(), event_type=event_type
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _read(used: int) -> bytes:
    return _wire(
        ModelHostDiskRead(
            correlation_id="c1",
            host_name=HOST,
            read_at=NOW - dt.timedelta(seconds=30),
            volumes=(
                ModelHostDiskVolume(
                    path=VOLUME, total_bytes=100, free_bytes=100 - used, used_bytes=used
                ),
            ),
            classes=(
                ModelDiskClassReading(
                    class_name="uv_cache", status="unread", unread_reason="not read"
                ),
            ),
        ).model_dump(mode="json"),
        "omnimarket.host-disk-read",
    )


def _request(caller_uuid: UUID) -> bytes:
    inject = _resolve_node_topics(NODE)[3]
    payload = _normalize_payload({}, caller_uuid, inject)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=payload,
        correlation_id=caller_uuid,
        source_tool="onex.run-node",
        target_tool=NODE,
    )
    return envelope.model_dump_json().encode("utf-8")


@pytest.fixture(autouse=True)
def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = tmp_path / "overlays" / NODE
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    HOST_DISK_READINGS._entries.clear()
    yield
    HOST_DISK_READINGS._entries.clear()


async def _drive(
    *inputs: tuple[str, bytes],
) -> dict[str, list[dict[str, Any]]]:
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="omn24610", group="omn24610")
    await bus.start()
    seen: dict[str, list[dict[str, Any]]] = {RUN_TOPIC: [], SCHEDULE_TOPIC: []}

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            body: dict[str, Any] = json.loads(bytes(getattr(message, "value", b"")))
            seen[topic].append(body)

        return _collect

    for topic in seen:
        await bus.subscribe(
            topic, on_message=_collector(topic), group_id=f"probe-{topic}"
        )
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()
    for topic, wire in inputs:
        await bus.publish(topic, None, wire, None)
        await asyncio.sleep(0.5)
    await asyncio.sleep(1.5)
    await bus.close()
    return seen


def _match(seen: dict[str, list[dict[str, Any]]], caller_uuid: UUID) -> dict[str, Any]:
    for data in seen[RUN_TOPIC]:
        if data.get("correlation_id") == str(caller_uuid):
            return data
    raise AssertionError(f"no run completed message for {caller_uuid}")


async def test_golden_on_demand_request_yields_one_completed_run_under_the_caller_id() -> (
    None
):
    caller = uuid.uuid4()
    seen = await _drive((READ_TOPIC, _read(90)), (REQUEST_TOPIC, _request(caller)))

    run = _match(seen, caller)
    assert run["payload"]["outcome"] == "completed"
    assert run["payload"]["trigger"] == "on_demand"
    assert {u["outcome"] for u in run["payload"]["units"]} == {"completed"}
    assert seen[SCHEDULE_TOPIC] == []


async def test_error_chain_a_missing_overlay_is_refused_under_the_caller_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    caller = uuid.uuid4()
    seen = await _drive((READ_TOPIC, _read(90)), (REQUEST_TOPIC, _request(caller)))

    run = _match(seen, caller)
    assert run["payload"]["outcome"] == "refused"


async def test_error_chain_a_failing_cleanup_fails_the_run_under_the_caller_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = InMemoryHostHygieneSender(
        {HOST: (EnumLabWorkUnitStatus.COMPLETED, 2, "exit 2")}
    )
    monkeypatch.setattr(run_requested_module, "build_sender", lambda _overlay: sender)
    caller = uuid.uuid4()
    seen = await _drive((READ_TOPIC, _read(90)), (REQUEST_TOPIC, _request(caller)))

    run = _match(seen, caller)
    assert run["payload"]["outcome"] == "failed"
    assert {u["outcome"] for u in run["payload"]["units"]} == {"failed"}


async def test_two_requests_with_different_caller_ids_are_each_answered_under_their_own_id() -> (
    None
):
    caller_a = uuid.uuid4()
    caller_b = uuid.uuid4()
    seen = await _drive(
        (READ_TOPIC, _read(90)),
        (REQUEST_TOPIC, _request(caller_a)),
        (REQUEST_TOPIC, _request(caller_b)),
    )

    run_a = _match(seen, caller_a)
    run_b = _match(seen, caller_b)
    assert run_a["correlation_id"] == str(caller_a)
    assert run_b["correlation_id"] == str(caller_b)
    assert run_a is not run_b


async def test_a_request_on_the_old_first_topic_produces_no_run_completed_message() -> (
    None
):
    caller = uuid.uuid4()
    seen = await _drive((READ_TOPIC, _read(90)), (READ_TOPIC, _request(caller)))

    assert seen[RUN_TOPIC] == []
