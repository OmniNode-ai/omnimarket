# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain stores: in memory (the default), a durable JSON-lines file, and a compacted Redpanda topic keyed by host.

Every store keeps the latest record per host. A record that does not parse is skipped and counted, never fixed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from omnimarket.nodes.node_host_drain_effect.overlay import ModelRedpandaSettings


class InMemoryDrainStore:
    def __init__(self) -> None:
        self._draining: dict[str, bool] = {}

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        self._draining[host_name] = draining

    def is_draining(self, host_name: str) -> bool:
        return self._draining.get(host_name, False)

    def draining_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(h for h, d in self._draining.items() if d))


def _record(
    host_name: str, draining: bool, reason: str, at: datetime
) -> dict[str, object]:
    return {
        "host": host_name,
        "draining": draining,
        "reason": reason,
        "at": at.isoformat(),
    }


def _fold(records: list[dict[str, object]]) -> dict[str, bool]:
    state: dict[str, bool] = {}
    for rec in records:
        host = rec.get("host")
        flag = rec.get("draining")
        if isinstance(host, str) and isinstance(flag, bool):
            state[host] = flag
    return state


class JsonlDrainStore:
    """Append-only file of drain records; the state is the fold of the file, so a restart keeps it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.skipped_lines = 0

    def _read(self) -> dict[str, bool]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        records: list[dict[str, object]] = []
        self.skipped_lines = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                self.skipped_lines += 1
        return _fold(records)

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_record(host_name, draining, reason, at)) + "\n")

    def is_draining(self, host_name: str) -> bool:
        return self._read().get(host_name, False)

    def draining_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(h for h, d in self._read().items() if d))


class ProtocolStateTopicClient(Protocol):
    """The two operations the Redpanda store needs of the bus, so a test supplies a fake broker."""

    async def send(self, topic: str, key: str, value: bytes) -> None: ...

    async def read_all(self, topic: str) -> list[tuple[str, bytes]]: ...


class AiokafkaStateTopicClient:
    """The real client: aiokafka against the broker the overlay's environment variable names."""

    def __init__(self, settings: ModelRedpandaSettings, bootstrap_servers: str) -> None:
        self._settings = settings
        self._bootstrap = bootstrap_servers

    async def send(self, topic: str, key: str, value: bytes) -> None:
        from aiokafka import AIOKafkaProducer
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._settings.client_id,
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await producer.start()
        try:
            await asyncio.wait_for(
                producer.send_and_wait(topic, value=value, key=key.encode("utf-8")),
                timeout=self._settings.send_timeout_seconds,
            )
        finally:
            await producer.stop()

    @staticmethod
    async def _behind(
        consumer: Any, assigned: list[Any], ends: Mapping[Any, int]
    ) -> bool:
        """True while some partition's position is short of its end offset read at the start."""
        for tp in assigned:
            if await consumer.position(tp) < ends[tp]:
                return True
        return False

    async def read_all(self, topic: str) -> list[tuple[str, bytes]]:
        from aiokafka import AIOKafkaConsumer, TopicPartition
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap,
            client_id=self._settings.client_id,
            enable_auto_commit=False,
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await consumer.start()
        try:
            partitions = await consumer.partitions_for_topic(topic) or set()
            assigned = [TopicPartition(topic, p) for p in sorted(partitions)]
            consumer.assign(assigned)
            await consumer.seek_to_beginning(*assigned)
            ends = await consumer.end_offsets(assigned)
            rows: list[tuple[str, bytes]] = []
            deadline = self._settings.read_timeout_seconds * 1000
            while await self._behind(consumer, assigned, ends):
                batch = await consumer.getmany(timeout_ms=deadline)
                if not batch:
                    raise TimeoutError("the state topic did not reach its end offsets")
                for records in batch.values():
                    for rec in records:
                        if rec.key is not None and rec.value is not None:
                            rows.append((rec.key.decode("utf-8"), rec.value))
            return rows
        finally:
            await consumer.stop()


class RedpandaDrainStore:
    """The drain state on a compacted topic, one record per host; the state is the fold of the topic."""

    def __init__(
        self, settings: ModelRedpandaSettings, client: ProtocolStateTopicClient
    ) -> None:
        self._topic = settings.state_topic
        self._client = client

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        value = json.dumps(_record(host_name, draining, reason, at)).encode("utf-8")
        asyncio.run(self._client.send(self._topic, host_name, value))

    def _state(self) -> dict[str, bool]:
        rows = asyncio.run(self._client.read_all(self._topic))
        records: list[dict[str, object]] = []
        for _key, value in rows:
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return _fold(records)

    def is_draining(self, host_name: str) -> bool:
        return self._state().get(host_name, False)

    def draining_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(h for h, d in self._state().items() if d))
