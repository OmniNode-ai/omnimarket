# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Opt-in Redpanda proof for node_handoff_check_effect (OMN-21910).

The same command-in, completed-event-out chain as the in-memory golden chain, over a real
broker of the declared test bus lane, on disposable topics. Behind ``RUN_REDPANDA_HANDOFF_CHECK=1``
and a reachable broker; the default suite never needs infrastructure. The node declares no
deployment overlay of its own: the broker comes from the shared test-lane resolution.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from omnimarket.nodes.node_handoff_check_effect.handlers.handler_handoff_check import (
    HandlerHandoffCheck,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelHandoffCheckRequest,
    ModelHandoffCheckResult,
)
from tests.bus_lane_guard import (
    assert_broker_in_declared_lane,
    resolve_test_bus_broker,
)

pytestmark = [pytest.mark.kafka, pytest.mark.integration]

_FLAG = "RUN_REDPANDA_HANDOFF_CHECK"
_COMMAND_TOPIC = "onex.cmd.omnimarket.handoff-check-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.handoff-check-completed.v1"
_NOW = "2026-11-01T23:00:00Z"
_RULING = (
    '2026-11-01T20:00:00Z | RULING | lane=orch | ticket=TKT-1 | question=planted | "go"'
)


async def _probe_broker(bootstrap: str) -> None:
    if os.environ.get(_FLAG) != "1":
        pytest.skip(f"set {_FLAG}=1 to run the handoff check over Redpanda")
    from aiokafka import AIOKafkaProducer

    assert_broker_in_declared_lane(bootstrap)
    try:
        probe = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await asyncio.wait_for(probe.start(), timeout=5)
        await probe.stop()
    except Exception as exc:
        pytest.skip(f"Redpanda broker not reachable at {bootstrap}: {exc}")


async def test_a_planted_ruling_is_refused_over_redpanda(tmp_path: Path) -> None:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    bootstrap = resolve_test_bus_broker()
    await _probe_broker(bootstrap)
    ledger = tmp_path / "ledger.md"
    ledger.write_text(_RULING + "\n", encoding="utf-8")
    probes = tmp_path / "probes.json"
    probes.write_text("[]", encoding="utf-8")
    request = ModelHandoffCheckRequest(
        command=EnumHandoffCommand.ASSEMBLE,
        ledger=str(ledger),
        archive_dir=str(tmp_path / "none"),
        now=_NOW,
        probes=str(probes),
    )
    suffix = uuid.uuid4().hex[:8]
    command_topic = f"{_COMMAND_TOPIC}.it-{suffix}"
    completed_topic = f"{_COMPLETED_TOPIC}.it-{suffix}"
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    command_consumer = AIOKafkaConsumer(
        command_topic,
        bootstrap_servers=bootstrap,
        group_id=f"handoff-check-command-{suffix}",
        auto_offset_reset="earliest",
    )
    completed_consumer = AIOKafkaConsumer(
        completed_topic,
        bootstrap_servers=bootstrap,
        group_id=f"handoff-check-completed-{suffix}",
        auto_offset_reset="earliest",
    )
    await producer.start()
    await command_consumer.start()
    await completed_consumer.start()
    try:
        await producer.send_and_wait(
            command_topic, value=request.model_dump_json().encode()
        )
        message = await asyncio.wait_for(command_consumer.getone(), timeout=15)
        result = HandlerHandoffCheck().handle(
            ModelHandoffCheckRequest.model_validate(json.loads(message.value))
        )
        await producer.send_and_wait(
            completed_topic, value=result.model_dump_json().encode()
        )
        done = await asyncio.wait_for(completed_consumer.getone(), timeout=15)
    finally:
        await completed_consumer.stop()
        await command_consumer.stop()
        await producer.stop()
    completed = ModelHandoffCheckResult.model_validate_json(done.value)
    assert completed.exit_code == 1
    assert len(completed.refused) == 1
