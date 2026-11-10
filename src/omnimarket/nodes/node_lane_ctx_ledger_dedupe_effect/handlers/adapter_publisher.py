# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event transport: Kafka when configured, topic-addressed JSON on stderr otherwise; and a recorder for tests."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel

from omnimarket.topic_namespace import apply_topic_namespace


def contract_topics() -> tuple[str, str, str]:
    """The refused topic, the completed topic and the terminal event, from this node's contract."""
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text(encoding="utf-8")
    )
    refused, completed = (str(t) for t in contract["event_bus"]["publish_topics"])
    return refused, completed, str(contract["terminal_event"])


class RecordingPublisher:
    """The in-memory default: keeps every event with its topic."""

    def __init__(self) -> None:
        self.events: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.events.append((topic, event))


class ContractEventPublisher:
    """Buffer a run's events and flush them on the contract's terminal event."""

    def __init__(self) -> None:
        self._buffer: list[tuple[str, BaseModel]] = []
        self._topics = contract_topics()

    def publish(self, topic: str, event: BaseModel) -> None:
        if topic not in self._topics[:2]:
            raise ValueError("undeclared publish topic")
        brokers = os.environ.get("KAFKA_BROKERS")
        if brokers:
            self._buffer.append((topic, event))
            if topic == self._topics[2]:
                batch, self._buffer = self._buffer, []
                asyncio.run(self._publish_kafka(brokers, batch))
        else:
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
            key = socket.gethostname().encode()
            for topic, event in batch:
                await producer.send_and_wait(
                    apply_topic_namespace(topic),
                    event.model_dump_json().encode(),
                    key=key,
                )
        finally:
            await producer.stop()
