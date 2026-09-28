# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI event transport: Kafka when configured, topic-addressed JSON otherwise."""

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel


def publish_topics() -> tuple[str, str]:
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text()
    )
    topics = contract["event_bus"]["publish_topics"]
    return str(topics[0]), str(topics[1])


class ContractEventPublisher:
    """Buffers a run's events and sends them in one producer session.

    A run decides hundreds of trees; one producer start per event would cost
    more than the scan. The run-completed topic (the contract's second publish
    topic, always the last event of a run) flushes the buffer.
    """

    def __init__(self) -> None:
        self._buffer: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        topics = publish_topics()
        if topic not in topics:
            raise ValueError("undeclared publish topic")
        brokers = os.environ.get("KAFKA_BROKERS")
        if brokers:
            self._buffer.append((topic, event))
            if topic == topics[1]:
                batch, self._buffer = self._buffer, []
                asyncio.run(self._publish_kafka(brokers, batch))
        else:
            # A standalone CLI has no runtime bus. Emit the same topic-addressed
            # events to stderr while stdout remains the typed run result.
            sys.stderr.write(
                json.dumps({"topic": topic, "event": event.model_dump(mode="json")})
                + "\n"
            )

    async def _publish_kafka(
        self, brokers: str, batch: list[tuple[str, BaseModel]]
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
            for topic, event in batch:
                await producer.send_and_wait(
                    topic,
                    event.model_dump_json().encode(),
                    key=str(event.model_dump()["host"]).encode(),
                )
        finally:
            await producer.stop()
