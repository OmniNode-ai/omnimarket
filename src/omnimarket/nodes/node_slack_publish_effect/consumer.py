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
from pathlib import Path
from typing import Any
from uuid import UUID

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_slack_publish_effect.handlers.handler_slack_publish_effect import (
    HandlerSlackPublishEffect,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

_log = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def _resolve_topics() -> tuple[str, str, str, str]:
    """Resolve (cmd, published, failed, deduped) topics from the contract.

    Topics are declared only in ``contract.yaml`` (event_bus.subscribe_topics /
    publish_topics); the consumer reads them at runtime so no topic literal lives
    in source. Fails fast if the contract does not declare the expected topics.
    """
    subscribe = contract_subscribe_topics(_CONTRACT_PATH)
    publish = contract_publish_topics(_CONTRACT_PATH)
    if not subscribe:
        raise ValueError(f"{_CONTRACT_PATH} declares no subscribe_topics")
    cmd_topic = subscribe[0]

    failed = next((t for t in publish if "failed" in t), None)
    deduped = next((t for t in publish if "deduped" in t), None)
    published = next(
        (t for t in publish if "failed" not in t and "deduped" not in t), None
    )
    if published is None or failed is None or deduped is None:
        raise ValueError(
            f"{_CONTRACT_PATH} event_bus.publish_topics must declare published, "
            f"failed and deduped outcome topics; got {publish!r}"
        )
    return cmd_topic, published, failed, deduped


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

    topic_cmd, topic_published, topic_failed, topic_deduped = _resolve_topics()

    consumer = AIOKafkaConsumer(
        topic_cmd,
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
        topic_cmd,
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
                        topic = topic_deduped
                    elif result_dict.get("success"):
                        topic = topic_published
                    else:
                        topic = topic_failed
                else:
                    topic = topic_published
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
                        topic_failed,
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
