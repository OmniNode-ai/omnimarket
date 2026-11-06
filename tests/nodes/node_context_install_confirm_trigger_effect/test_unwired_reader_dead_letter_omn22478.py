# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: the recorded dead-letter record, through the runtime's own dispatch.

Loads the trigger node's real contract from disk, boots it through
``wire_from_manifest`` (the call the kernel makes) with an in-memory bus, publishes the
recorded ``projection-session-content-applied`` payload on the contract's subscribe
topic and reads what the runtime did with it. Only the bus is in-memory; the contract,
handler construction and dispatch engine are the runtime's own.

``RECORDED_PAYLOAD`` is the ``payload`` of one real dead-letter record (offset
23357700 of ``onex.dlq.omnibase-infra.events.v1``, 2026-11-06T09:48:58Z). On the base
source the dispatch raised ``handle() missing 1 required keyword-only argument:
'reader'`` for it and the record was dead-lettered.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.report import (
    ModelAutoWiringReport as ModelAutoWiringReport,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_install_confirm_trigger_effect/contract.yaml"
)
RECORDED_PAYLOAD = (
    '{"rows_upserted":1,"event_id":'
    '"1a1feaba4b4ad77234eae5cc83c8d5336c86f6835f3cd8b40f861a0738a4b42f",'
    '"projected":true}'
)


async def _drive(
    payload: dict[str, object] | None = None,
) -> tuple[ModelAutoWiringReport, list[tuple[str, bytes]]]:
    contract = yaml.safe_load(CONTRACT.read_text())
    applied_topic = contract["event_bus"]["subscribe_topics"][0]
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="trigger-boot", group="trigger-boot")
    await bus.start()
    published: list[tuple[str, bytes]] = []
    done = asyncio.Event()

    async def _record(message: object) -> None:
        published.append(
            (str(getattr(message, "topic", "")), bytes(getattr(message, "value", b"")))
        )
        done.set()

    for topic in contract["event_bus"]["publish_topics"]:
        await bus.subscribe(topic, on_message=_record, group_id=f"probe-{topic}")

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    engine.freeze()

    now = datetime(2026, 11, 6, 9, 48, 58, tzinfo=UTC)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=payload if payload is not None else json.loads(RECORDED_PAYLOAD),
        correlation_id=uuid4(),
        envelope_timestamp=now,
    )
    await bus.publish(applied_topic, None, envelope.model_dump_json().encode(), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(done.wait(), timeout=3)
    await asyncio.sleep(0.5)
    await bus.close()
    return report, published


async def test_golden_recorded_record_yields_one_skip_event_and_nothing_completed() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text())
    completed = contract["terminal_event"]
    skipped = contract["published_events"][0]["topic"]

    report, published = await _drive()

    assert report.results[0].outcome.value == "wired"
    assert [topic for topic, _ in published] == [skipped]
    payload = json.loads(published[0][1])["payload"]
    assert payload == {
        "event_id": json.loads(RECORDED_PAYLOAD)["event_id"],
        "reason": "source_unavailable",
        "dependency": "ProtocolSessionCompactSource",
    }
    assert completed not in [topic for topic, _ in published]


async def test_error_a_payload_the_model_refuses_publishes_no_skip() -> None:
    _, published = await _drive(payload={"event_id": "e-1", "unexpected": True})

    assert published == []
