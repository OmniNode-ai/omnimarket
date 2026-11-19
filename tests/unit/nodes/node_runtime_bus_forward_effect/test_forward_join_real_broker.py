# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-broker leg of the S1 join across the runtime-bus forwarder seam (OMN-24003).

Where the in-memory leg (``test_forward_join_message_id.py``, whose recorded-event stand-in this leg reuses)
judges the join on a recording publisher, this leg carries one recorded event through the real forwarder — its
overlay, its lane resolution, its confluent-kafka producer — onto the declared lane's broker, and reads the
record back with a confluent-kafka consumer. The consumer ``assign``s every partition of the topic at its
current high watermark instead of ``subscribe``ing, so it needs no consumer-group grant from the lab logins
and no record written before this run can be mistaken for this run's; the read-back then waits only for the
one record whose ``message-id`` header is this run's own, and judges the bytes against the bytes forwarded.

The real-broker leg of the S1 join, run on a lab host with
`FEDERATION_JOIN_LANE=dev OMNIBASE_PATH=<workspace root> pytest tests/unit/nodes/node_runtime_bus_forward_effect/test_forward_join_real_broker.py -m kafka -q`;
it is deselected in CI by the marker.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid

import confluent_kafka
import pytest

from omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder import (
    CoreRuntimeBusForwarder,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    ModelRuntimeBusForwardOverlay,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.publishers.publisher_kafka import (
    broker_for_lane,
)
from tests.unit.nodes.node_runtime_bus_forward_effect.test_forward_join_message_id import (
    RecordedRuntimeBusEvent,
)

pytestmark = pytest.mark.unit

# The declared bus lane the real-broker leg runs on; its broker resolves from the lane declaration under
# OMNIBASE_PATH, the way onex delegate and onex scm resolve it.
LANE_ENV = "FEDERATION_JOIN_LANE"
ROOT_ENV = "OMNIBASE_PATH"
# The host's SASL credential, passed through to the lane resolution only when the shell carries it.
SASL_ENVIRONMENT = ("KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD")
# The record topic this leg writes and reads back on (the lab logins may write and read it on lane dev); it
# sits under the overlay allow-list on purpose.
JOIN_TOPIC = "onex.evt.omnimarket.pr-landing-liveness-join-recorded.v1"
JOIN_GROUP_PREFIX = "federation-join-"
MESSAGE_ID_HEADER = "message-id"
# The consumer sits at the high watermark before the forward, so the read-back waits for this run's one
# record and nothing older; this is the ceiling on that wait, not a broker setting.
READ_BACK_SECONDS = 20.0
# The metadata and watermark queries of the self-assignment, bounded the same way.
SEEK_METADATA_SECONDS = 10.0


def _lane_environ(root: str) -> dict[str, str]:
    """The environment the lane resolution reads: the workspace root, plus the host's SASL credential when set."""
    return {
        ROOT_ENV: root,
        **{name: os.environ[name] for name in SASL_ENVIRONMENT if os.environ.get(name)},
    }


def _assign_at_high_watermark(consumer: confluent_kafka.Consumer, topic: str) -> None:
    """Put the consumer on every partition at its current high watermark: no group grant, no earlier record."""
    metadata = consumer.list_topics(topic, timeout=SEEK_METADATA_SECONDS)
    partitions = sorted(metadata.topics[topic].partitions)
    if not partitions:
        pytest.fail(f"the join topic holds no partitions on the lane broker: {topic}")
    assignment = []
    for partition in partitions:
        _low, high = consumer.get_watermark_offsets(
            topic, partition, timeout=SEEK_METADATA_SECONDS, cached=False
        )
        assignment.append(confluent_kafka.TopicPartition(topic, partition, offset=high))
    consumer.assign(assignment)


def _read_back_own_record(
    consumer: confluent_kafka.Consumer,
    *,
    message_id: str,
    key_bytes: bytes,
    value_bytes: bytes,
) -> None:
    """Poll until the record with this run's ``message-id`` header arrives, and judge its bytes."""
    message_id_bytes = message_id.encode("utf-8")
    deadline = time.monotonic() + READ_BACK_SECONDS
    while time.monotonic() < deadline:
        message = consumer.poll(timeout=1.0)
        if message is None or message.error() is not None:
            continue
        headers = dict(message.headers() or ())
        if headers.get(MESSAGE_ID_HEADER) != message_id_bytes:
            continue
        assert message.key() == key_bytes
        assert (
            message.value() == value_bytes
        )  # bytes on the wire, compared as bytes, never parsed
        return
    pytest.fail(
        f"the forwarded record never reached {JOIN_TOPIC} within {READ_BACK_SECONDS} seconds"
    )


@pytest.mark.kafka
@pytest.mark.integration
def test_join_real_broker_the_forwarder_carries_a_recorded_event_onto_the_declared_lane() -> (
    None
):
    """One recorded event forwarded through the real seam is on the lane broker, bytes and identity intact."""
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    broker = broker_for_lane(lane, _lane_environ(root))
    bootstrap_servers = broker.bootstrap_servers
    assert bootstrap_servers, "a kafka broker always carries its bootstrap address"

    run = uuid.uuid4().hex
    message_id = str(uuid.uuid4())
    key_bytes = run.encode("utf-8")
    value_bytes = json.dumps(
        {"kind": "example", "tenant": "example-tenant-a", "run": run}
    ).encode("utf-8")
    recorded = RecordedRuntimeBusEvent(
        topic=JOIN_TOPIC,
        key_b64=base64.b64encode(key_bytes).decode("ascii"),
        value_b64=base64.b64encode(value_bytes).decode("ascii"),
        headers={MESSAGE_ID_HEADER: message_id},
    )

    consumer = confluent_kafka.Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            **broker.client_security(),
            "group.id": f"{JOIN_GROUP_PREFIX}{run}",
            "enable.auto.commit": False,
            "auto.offset.reset": "latest",
        }
    )
    try:
        _assign_at_high_watermark(consumer, JOIN_TOPIC)
        receipt = CoreRuntimeBusForwarder(
            overlay=ModelRuntimeBusForwardOverlay(
                event_bus={"type": "kafka", "lane": lane},
                max_events=5,
                flush_timeout_seconds=15,
                allowed_topic_prefixes=("onex.evt.omnimarket.",),
            ),
            environ={ROOT_ENV: root},
        ).forward([recorded])
        assert receipt == {"forwarded": 1, "failed": 0, "detail": None}
        _read_back_own_record(
            consumer,
            message_id=message_id,
            key_bytes=key_bytes,
            value_bytes=value_bytes,
        )
    finally:
        consumer.close()
