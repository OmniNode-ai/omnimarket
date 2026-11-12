# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reap effect with its real adapters on a real broker: delete, read back, refuse (OMN-23992).

Two real groups are left Empty by closing their consumers and a third stays live. A reducer-decided command deletes the Empty
two and reads them back absent while the live group is untouched; the same command decided from a file is refused and every
group remains. It runs against the plaintext lab broker the caller started for the proof (never the dev lane's):

    CONSUMER_GROUP_PARITY_BOOTSTRAP=<host:port> uv run pytest tests/nodes/node_consumer_group_reap_effect/test_adapter_broker.py -m kafka -q
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from omnimarket.nodes.node_consumer_group_reap_effect.adapters.kafka_group_admin import (
    KafkaGroupAdmin,
)
from omnimarket.nodes.node_consumer_group_reap_effect.handlers.handler_group_reap import (
    HandlerGroupReap,
)
from omnimarket.nodes.node_consumer_group_reap_effect.models import EnumReapRefusal
from tests.nodes.node_consumer_group_reap_effect.support import (
    command,
    due_record,
    execution,
    policy,
)

BOOTSTRAP_ENV = "CONSUMER_GROUP_PARITY_BOOTSTRAP"
WAIT_S = 30.0


def join(bootstrap: str, topic: str, group: str) -> object:
    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([topic])
    deadline = time.monotonic() + WAIT_S
    while not consumer.assignment() and time.monotonic() < deadline:
        consumer.poll(0.5)
    assert consumer.assignment(), f"{group} never joined"
    return consumer


def wait_for(
    admin: KafkaGroupAdmin, config: dict[str, str], group: str, state: str
) -> None:
    deadline = time.monotonic() + WAIT_S
    while admin.list_groups(config).get(group) != state and time.monotonic() < deadline:
        time.sleep(0.5)
    assert admin.list_groups(config).get(group) == state


@pytest.mark.kafka
@pytest.mark.integration
def test_real_groups_are_deleted_read_back_and_a_file_decided_command_is_refused() -> (
    None
):
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    from confluent_kafka.admin import AdminClient, NewTopic

    config = {"bootstrap.servers": bootstrap}
    topic = f"consumer-group-reap-{uuid.uuid4().hex}"
    topic_admin = AdminClient(config)
    for future in topic_admin.create_topics([NewTopic(topic, 1, 1)]).values():
        future.result(timeout=WAIT_S)
    suffix = [uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]]
    due = [f"example.client.{s}" for s in suffix]
    live = "example.stable-group"
    admin = KafkaGroupAdmin(30.0)
    keeper = join(bootstrap, topic, live)
    try:
        for name in due:
            join(bootstrap, topic, name).close()  # type: ignore[attr-defined]
        for name in due:
            wait_for(admin, config, name, "EMPTY")
        wait_for(admin, config, live, "STABLE")
        handler = HandlerGroupReap(
            policy=policy(),
            overlay=execution(read_back_attempts=10, read_back_wait_seconds=1),
            lister=admin.list_groups,
            deleter=admin.delete_groups,
            health=admin.check_health,
            sleeper=time.sleep,
            resolve_broker=lambda _lane: ("broker", "environment", config),
        )
        # the record is built over the real group names, decided by the reducer's fold
        record = due_record(*due)
        file_decided = command(record).model_copy(update={"decided_by": "file"})
        refused = asyncio.run(handler.handle(file_decided))
        assert refused.status == "refused"
        assert {r.code for r in refused.refusals} == {
            EnumReapRefusal.DECIDED_NOT_BY_REDUCER
        }
        assert set(due) <= set(admin.list_groups(config))

        outcome = asyncio.run(handler.handle(command(record)))
        assert outcome.status == "reaped", outcome.detail
        assert set(outcome.deleted) == set(due)
        assert set(outcome.read_back_absent) == set(due)
        listing = admin.list_groups(config)
        assert not set(due) & set(listing)
        assert listing[live] == "STABLE"
    finally:
        keeper.close()  # type: ignore[attr-defined]
