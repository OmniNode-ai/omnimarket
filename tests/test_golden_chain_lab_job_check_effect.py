# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: runtime tick, sweep request, check effect, one lab-job-checked record per job.

Run on an in-memory bus. The schedule node is booted through
``wire_from_manifest`` and publishes the request on the contract's command
topic. The check effect's handler is called with each request it reads off
that topic (the orchestrator-side wiring that subscribes to it does not exist
yet), over a store answering from the recorded ledger window, and publishes its
records on the bus. The records are read back from the checked topic.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.enums.enum_lab_job import EnumLabJobState
from omnimarket.models.lab_job.model_lab_job_check import (
    ModelLabJobChecked,
    ModelLabJobCheckRequested,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.handler_lab_job_check_effect import (
    HandlerLabJobCheckEffect,
)
from omnimarket.nodes.node_lab_job_check_effect.models import ModelRelayReading
from tests.helpers.lab_job_check_fakes import ADOPTED, FakeStore, _at, _job

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
REQUEST_TOPIC = "onex.cmd.omnimarket.lab-job-check-requested.v1"
CHECKED_TOPIC = "onex.evt.omnimarket.lab-job-checked.v1"


def test_the_effect_publishes_exactly_the_checked_topic() -> None:
    contract = yaml.safe_load(
        (NODES / "node_lab_job_check_effect" / "contract.yaml").read_text()
    )
    assert contract["event_bus"]["publish_topics"] == [CHECKED_TOPIC]
    assert contract["terminal_event"] == CHECKED_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [REQUEST_TOPIC]


def _tick_wire(now: dt.datetime) -> bytes:
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="golden-chain",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
    )
    return envelope.model_dump_json().encode("utf-8")


async def test_a_window_tick_ends_as_one_checked_record_per_open_job() -> None:
    now = dt.datetime(2026, 10, 5, 5, 30, tzinfo=dt.UTC)
    jobs = [
        ADOPTED["fan"],
        ADOPTED["validator"],
        _job("lj-" + "1" * 16, EnumLabJobState.QUEUED, entered="2026-10-05T04:00:00Z"),
    ]
    store = FakeStore(
        jobs,
        relay=ModelRelayReading(
            last_event_at=_at("2026-10-05T05:29:30Z"), event_count=400
        ),
    )
    store.as_of = now

    manifest = discover_contracts_from_paths(
        [NODES / "node_lab_job_check_schedule_compute" / "contract.yaml"]
    )
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="lab-job-check", group="lab-job-check")
    await bus.start()
    requests: list[ModelLabJobCheckRequested] = []
    checked: list[ModelLabJobChecked] = []
    done = asyncio.Event()
    effect = HandlerLabJobCheckEffect(store, bus=cast(ProtocolLabRunBus, bus))

    async def on_request(message: object) -> None:
        body = json.loads(bytes(getattr(message, "value", b"")))
        request = ModelLabJobCheckRequested.model_validate(body["payload"])
        requests.append(request)
        await effect.handle(request)

    async def on_checked(message: object) -> None:
        body = json.loads(bytes(getattr(message, "value", b"")))
        checked.append(ModelLabJobChecked.model_validate(body["payload"]))
        if len(checked) == len(jobs):
            done.set()

    await bus.subscribe(REQUEST_TOPIC, on_message=on_request, group_id="effect")
    await bus.subscribe(CHECKED_TOPIC, on_message=on_checked, group_id="probe")

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()
    await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(done.wait(), timeout=30)
    await bus.close()

    assert len(requests) == 1
    assert requests[0].requested_at == now
    assert {c.job_id for c in checked} == {j.job_id for j in jobs}
    by_id = {c.job_id: c for c in checked}
    assert by_id[ADOPTED["fan"].job_id].job_state is EnumLabJobState.RUNNING
    # The queued job has been waiting 90 minutes: its dispatch deadline has elapsed.
    assert [d.value for d in by_id["lj-" + "1" * 16].elapsed_deadlines] == ["dispatch"]
    assert all(c.check_id == requests[0].check_id for c in checked)
