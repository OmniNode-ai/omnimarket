# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the scored event: Kafka when configured, topic-addressed JSON otherwise."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

from omnimarket.nodes.node_reviewer_eval_effect.models import ModelReviewerEvalScored
from omnimarket.topic_namespace import apply_topic_namespace


def scored_topic() -> str:
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return str(contract["terminal_event"])


def publish_scored(event: ModelReviewerEvalScored) -> str:
    """Publish ``event`` on the contract's terminal topic and return the topic."""
    topic = scored_topic()
    brokers = os.environ.get("KAFKA_BROKERS")
    if brokers:
        asyncio.run(_publish_kafka(brokers, topic, event))
    else:
        # A standalone run has no bus: the same topic-addressed event goes to
        # stderr while stdout stays the typed result.
        sys.stderr.write(
            json.dumps({"topic": topic, "event": event.model_dump(mode="json")}) + "\n"
        )
    return topic


async def _publish_kafka(
    brokers: str, topic: str, event: ModelReviewerEvalScored
) -> None:
    from aiokafka import AIOKafkaProducer
    from omnibase_infra.event_bus.kafka_auth import (
        build_aiokafka_auth_kwargs_from_env,
    )

    producer = AIOKafkaProducer(
        bootstrap_servers=brokers, **build_aiokafka_auth_kwargs_from_env()
    )
    await producer.start()
    try:
        await producer.send_and_wait(
            apply_topic_namespace(topic),
            event.model_dump_json().encode(),
            key=event.correlation_id.encode(),
        )
    finally:
        await producer.stop()
