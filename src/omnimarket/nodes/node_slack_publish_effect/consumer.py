# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer entry point for node_slack_publish_effect (OMN-13723).

Subscribes to ``onex.cmd.omnimarket.slack-publish.v1`` via aiokafka, invokes
``HandlerSlackPublishEffect``, and emits the appropriate terminal event.

Environment variables:
    KAFKA_BOOTSTRAP_SERVERS  Kafka/Redpanda bootstrap server (required).
    KAFKA_BROKER             Alias for KAFKA_BOOTSTRAP_SERVERS (legacy compat).
    ONEX_STATE_DIR           Failure-state + ledger dir (default: ~/.onex_state).
    ONEX_STATE_ROOT          Test isolation override for state root.

Usage (standalone consumer loop):
    python -m omnimarket.nodes.node_slack_publish_effect.consumer

The loop runs until SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Any
from uuid import UUID

from omnimarket.nodes.node_slack_publish_effect.handlers.handler_slack_publish_effect import (
    HandlerSlackPublishEffect,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

_log = logging.getLogger(__name__)

_TOPIC_CMD = "onex.cmd.omnimarket.slack-publish.v1"
_TOPIC_PUBLISHED = "onex.evt.omnimarket.slack-published.v1"
_TOPIC_FAILED = "onex.evt.omnimarket.slack-publish-failed.v1"
_TOPIC_DEDUPED = "onex.evt.omnimarket.slack-publish-deduped.v1"


async def _run_consumer(broker: str, group_id: str) -> None:
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    except ImportError:
        _log.error(
            "aiokafka is not installed. Install with: uv add aiokafka. "
            "Cannot start Kafka consumer."
        )
        sys.exit(1)

    handler = HandlerSlackPublishEffect()

    consumer = AIOKafkaConsumer(
        _TOPIC_CMD,
        bootstrap_servers=broker,
        group_id=group_id,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=broker,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
    )

    await consumer.start()
    await producer.start()
    _log.info(
        "slack-publish-effect consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        _TOPIC_CMD,
    )

    stop_event = asyncio.Event()

    def _signal_handler(sig: int, _: Any) -> None:
        _log.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    try:
        async for msg in consumer:
            if stop_event.is_set():
                break

            raw: dict[str, Any] = msg.value if isinstance(msg.value, dict) else {}
            correlation_id_raw = raw.get("correlation_id", "")
            _log.info(
                "received slack-publish command correlation_id=%s", correlation_id_raw
            )

            try:
                command = ModelSlackPublish(
                    channel=raw["channel"],
                    blocks=raw.get("blocks"),
                    text=raw.get("text"),
                    thread_ts=raw.get("thread_ts"),
                    idempotency_key=raw["idempotency_key"],
                    correlation_id=UUID(str(correlation_id_raw)),
                )
                output = await handler.handle(command)

                events = output.events or ()
                result_event = events[0] if events else None

                if result_event is not None:
                    result_dict = result_event.model_dump()
                    if result_dict.get("deduped"):
                        topic = _TOPIC_DEDUPED
                    elif result_dict.get("success"):
                        topic = _TOPIC_PUBLISHED
                    else:
                        topic = _TOPIC_FAILED
                else:
                    topic = _TOPIC_PUBLISHED
                    result_dict = {
                        "correlation_id": str(correlation_id_raw),
                        "success": True,
                    }

                await producer.send_and_wait(topic, result_dict)
                _log.info(
                    "slack-publish outcome emitted topic=%s correlation_id=%s",
                    topic,
                    correlation_id_raw,
                )
            except Exception as exc:
                _log.error(
                    "slack-publish-effect failed for correlation_id=%s: %s",
                    correlation_id_raw,
                    exc,
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await producer.send_and_wait(
                        _TOPIC_FAILED,
                        {
                            "correlation_id": str(correlation_id_raw),
                            "success": False,
                            "error_code": "HANDLER_EXCEPTION",
                        },
                    )
    finally:
        await consumer.stop()
        await producer.stop()
        _log.info("slack-publish-effect consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
        "KAFKA_BROKER", ""
    )
    group_id = os.environ.get(
        "SLACK_PUBLISH_GROUP", "omnimarket.slack_publish_effect.consume.v1"
    )
    asyncio.run(_run_consumer(broker, group_id))


if __name__ == "__main__":
    main()
