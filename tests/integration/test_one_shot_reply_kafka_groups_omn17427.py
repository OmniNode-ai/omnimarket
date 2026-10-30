# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N one-shot SCM calls over a real broker add no consumer group (OMN-17427).

The Redpanda leg of tests/unit/handlers/scm/test_scm_bus_caller_leaves_no_group_omn17427.py:
the same caller code, the broker named by ``KAFKA_BOOTSTRAP_SERVERS`` (the e2e
compose stack by default). The broker's group list is read with an admin client
before and after, so what is measured is the broker's state and not the bus
object's.
"""

from __future__ import annotations

import json
import uuid

import pytest
from aiokafka.admin import AIOKafkaAdminClient
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from pydantic import BaseModel

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.scm import ModelScmTopics

RUN = uuid.uuid4().hex[:8]
TOPICS = ModelScmTopics(
    command=f"onex.cmd.omnimarket.one-shot-{RUN}-requested.v1",
    success=f"onex.evt.omnimarket.one-shot-{RUN}-completed.v1",
    failure=f"onex.evt.omnimarket.one-shot-{RUN}-failed.v1",
)
CALLS = 10


class _Request(BaseModel):
    verb: str = "pr-view"


async def _group_ids(bus: EventBusKafka) -> set[str]:
    admin = AIOKafkaAdminClient(
        bootstrap_servers=bus._bootstrap_servers, **bus._build_auth_kwargs()
    )
    await admin.start()
    try:
        return {group[0] for group in await admin.list_consumer_groups()}
    finally:
        await admin.close()


@pytest.mark.integration
@pytest.mark.kafka
async def test_n_one_shot_calls_leave_the_broker_group_count_unchanged(
    kafka_integration_bus: EventBusKafka,
) -> None:
    bus = kafka_integration_bus

    async def on_command(message: ProtocolBusMessage) -> None:
        raw = json.loads(message.value)
        reply = ModelEventEnvelope[dict[str, object]](
            payload={"outcome": "completed"},
            correlation_id=raw["correlation_id"],
            parent_envelope_id=raw["envelope_id"],
            event_type="omnimarket.one-shot-completed",
        )
        await bus.publish(
            TOPICS.success, None, json.dumps(reply.model_dump(mode="json")).encode()
        )

    stop_host = await bus.subscribe(
        TOPICS.command, on_message=on_command, group_id=f"one-shot-host-{RUN}"
    )
    try:
        before = await _group_ids(bus)
        for _ in range(CALLS):
            terminal = await ScmBusCaller(bus).call_topics(
                topics=TOPICS,
                request=_Request(),
                correlation_id=uuid.uuid4(),
                timeout_s=20,
            )
            assert terminal.payload == {"outcome": "completed"}
        after = await _group_ids(bus)
        assert after == before
    finally:
        await stop_host()
