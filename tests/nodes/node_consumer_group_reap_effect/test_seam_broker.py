# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam on a real broker: census event, reducer record and reap command keep one shape on Redpanda (OMN-23992).

The in-memory legs of the seam live in ``test_seam_reducer.py`` and the guard effect's tests. This leg sends each hop of the
chain (census event, reducer record, reap command, reap outcome) through a real topic and judges the answer against the
in-process run. It runs against the plaintext lab broker the caller started for the proof (never the dev lane's):

    CONSUMER_GROUP_PARITY_BOOTSTRAP=<host:port> uv run pytest tests/nodes/node_consumer_group_reap_effect/test_seam_broker.py -m kafka -q
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from omnimarket.nodes.node_consumer_group_reap_effect.models import (
    ModelGroupReapCommand,
    ModelGroupReapOutcome,
)
from omnimarket.nodes.node_consumer_group_state_reducer.handlers.handler_group_state import (
    fold,
)
from omnimarket.nodes.node_consumer_group_state_reducer.models import (
    ModelGroupCensusEvent,
    ModelGroupStateRecord,
)
from tests.nodes.node_consumer_group_reap_effect.support import (
    GROUP_A,
    GROUP_B,
    STABLE,
    FakeBroker,
    build_handler,
    census,
    policy,
)

BOOTSTRAP_ENV = "CONSUMER_GROUP_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0


async def over_the_broker(bootstrap: str, topic: str, text: str) -> str:
    """Publish ``text`` to a fresh topic and read it back with a fresh consumer group."""
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap, security_protocol="PLAINTEXT"
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, text.encode())
    finally:
        await producer.stop()
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        security_protocol="PLAINTEXT",
        group_id=f"{topic}-{uuid.uuid4().hex}",
        auto_offset_reset="earliest",
    )
    await consumer.start()
    try:
        message = await asyncio.wait_for(consumer.getone(), TIMEOUT_S)
    finally:
        await consumer.stop()
    return message.value.decode()


async def chain(bootstrap: str | None) -> ModelGroupReapOutcome:
    """Census, fold, command and outcome; each hop crosses the broker when ``bootstrap`` names one."""
    prefix = f"consumer-group-parity-{uuid.uuid4().hex}"

    async def hop(name: str, text: str) -> str:
        return (
            text
            if bootstrap is None
            else await over_the_broker(bootstrap, f"{prefix}.{name}", text)
        )

    groups = {GROUP_A: "EMPTY", GROUP_B: "EMPTY", STABLE: "STABLE"}
    first = ModelGroupCensusEvent.model_validate_json(
        await hop("census-first", census(groups, 0).model_dump_json())
    )
    record = fold(None, first, policy())
    later = ModelGroupCensusEvent.model_validate_json(
        await hop(
            "census-later",
            census(groups, policy().empty_since_bound_seconds + 1).model_dump_json(),
        )
    )
    record = ModelGroupStateRecord.model_validate_json(
        await hop("state", fold(record, later, policy()).model_dump_json())
    )
    command = ModelGroupReapCommand.model_validate_json(
        await hop(
            "command",
            ModelGroupReapCommand.from_state_record(
                record, uuid.uuid4()
            ).model_dump_json(),
        )
    )
    outcome = await build_handler(FakeBroker(groups)).handle(command)
    return ModelGroupReapOutcome.model_validate_json(
        await hop("outcome", outcome.model_dump_json())
    )


def settled(outcome: ModelGroupReapOutcome) -> dict[str, object]:
    return outcome.model_dump(mode="json", exclude={"correlation_id"})


@pytest.mark.kafka
@pytest.mark.integration
def test_the_chain_reads_the_same_on_a_real_broker_as_in_process() -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    in_process = asyncio.run(chain(None))
    on_broker = asyncio.run(chain(bootstrap))
    assert in_process.status == "reaped"
    assert settled(on_broker) == settled(in_process)
