# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Redpanda profile: the same command-in, resolved-event-out chain over a real broker.

Disposable topics on the declared test bus lane. Behind ``RUN_REDPANDA_MACHINE_GRANT_ROLE=1`` and a
reachable broker; the default suite never needs infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_machine_grant_role_compute as node_package
from omnimarket.nodes.node_machine_grant_role_compute import HandlerMachineGrantRole
from omnimarket.nodes.node_machine_grant_role_compute.models import (
    ModelMachineGrantRoleRequest,
    ModelMachineGrantRoleResult,
)
from tests.bus_lane_guard import (
    assert_broker_in_declared_lane,
    resolve_test_bus_broker,
)
from tests.nodes.node_machine_grant_role_compute.overlay_fixture import TWO_WRITERS

pytestmark = [pytest.mark.kafka, pytest.mark.integration]

_FLAG = "RUN_REDPANDA_MACHINE_GRANT_ROLE"
_CONTRACT = yaml.safe_load(
    (Path(node_package.__file__).parent / "contract.yaml").read_text()
)
_COMMAND_TOPIC = _CONTRACT["runtime_dispatch"]["command_topic"]
_RESOLVED_TOPIC = _CONTRACT["terminal_event"]


async def _probe_broker(bootstrap: str) -> None:
    if os.environ.get(_FLAG) != "1":
        pytest.skip(f"set {_FLAG}=1 to run the grant role chain over Redpanda")
    from aiokafka import AIOKafkaProducer

    assert_broker_in_declared_lane(bootstrap)
    try:
        probe = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await asyncio.wait_for(probe.start(), timeout=5)
        await probe.stop()
    except Exception as exc:
        pytest.skip(f"Redpanda broker not reachable at {bootstrap}: {exc}")


async def test_scm_writer_bindings_resolve_over_redpanda() -> None:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    bootstrap = resolve_test_bus_broker()
    await _probe_broker(bootstrap)
    request = ModelMachineGrantRoleRequest(overlay=TWO_WRITERS, role="scm-writer")
    suffix = uuid.uuid4().hex[:8]
    command_topic = f"{_COMMAND_TOPIC}.it-{suffix}"
    resolved_topic = f"{_RESOLVED_TOPIC}.it-{suffix}"
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    command_consumer = AIOKafkaConsumer(
        command_topic,
        bootstrap_servers=bootstrap,
        group_id=f"machine-grant-role-command-{suffix}",
        auto_offset_reset="earliest",
    )
    resolved_consumer = AIOKafkaConsumer(
        resolved_topic,
        bootstrap_servers=bootstrap,
        group_id=f"machine-grant-role-resolved-{suffix}",
        auto_offset_reset="earliest",
    )
    await producer.start()
    await command_consumer.start()
    await resolved_consumer.start()
    try:
        await producer.send_and_wait(
            command_topic, value=request.model_dump_json().encode()
        )
        message = await asyncio.wait_for(command_consumer.getone(), timeout=15)
        result = HandlerMachineGrantRole().handle(
            ModelMachineGrantRoleRequest.model_validate(json.loads(message.value))
        )
        await producer.send_and_wait(
            resolved_topic, value=result.model_dump_json().encode()
        )
        done = await asyncio.wait_for(resolved_consumer.getone(), timeout=15)
    finally:
        await resolved_consumer.stop()
        await command_consumer.stop()
        await producer.stop()
    resolved = ModelMachineGrantRoleResult.model_validate_json(done.value)
    assert resolved.status == "resolved"
    assert [b.principal for b in resolved.bindings] == ["lab-host-a", "lab-host-b"]
