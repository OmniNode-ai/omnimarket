# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes for the topic archive and replay node tests.

The source topic, the sink, the cipher and the replay target stand in for the
live boundary behind the same protocols, so no broker, drive or key is needed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

from omnimarket.nodes.node_topic_archive_effect.handlers.handler_topic_archive import (
    contract_source_topics,
)
from omnimarket.topic_archive.models import EnumArchiveEncryption, ModelArchivedRecord

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
DAY1 = dt.datetime(2026, 9, 18, 23, 59, 59, 999000, tzinfo=dt.UTC)
DAY2 = dt.datetime(2026, 9, 19, 0, 0, 0, tzinfo=dt.UTC)


def ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


def rec(topic: str, offset: int, ts: dt.datetime, value: bytes) -> ModelArchivedRecord:
    return ModelArchivedRecord.from_bytes(
        topic=topic,
        partition=0,
        offset=offset,
        timestamp_ms=ms(ts),
        key=b"k",
        value=value,
        headers=[("correlation_id", b"c-1"), ("empty", None)],
    )


class FakeTopic:
    """In-memory source: one partition per topic, a movable log start."""

    def __init__(self, records: dict[str, list[ModelArchivedRecord]]) -> None:
        self.records = records
        self.low: dict[str, int] = {
            t: r[0].offset if r else 0 for t, r in records.items()
        }
        self.reads = 0

    async def partitions(self, topic: str) -> list[int]:
        return [0] if topic in self.records else []

    async def watermarks(self, topic: str, partition: int) -> tuple[int, int]:
        rs = self.records[topic]
        return (self.low[topic], rs[-1].offset + 1 if rs else 0)

    async def read_range(
        self, topic: str, partition: int, start: int, end_exclusive: int
    ) -> AsyncIterator[ModelArchivedRecord]:
        self.reads += 1
        for r in self.records[topic]:
            if max(start, self.low[topic]) <= r.offset < end_exclusive:
                yield r


class MemorySink:
    def __init__(self, *, requires_encryption: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.requires_encryption = requires_encryption
        self.location = "memory://test"

    def put(self, name: str, data: bytes) -> None:
        self.objects[name] = data

    def get(self, name: str) -> bytes:
        return self.objects[name]

    def exists(self, name: str) -> bool:
        return name in self.objects

    def list_names(self, prefix: str) -> list[str]:
        return sorted(n for n in self.objects if n.startswith(prefix))


class XorCipher:
    """Test-only reversible transform standing in for age."""

    encryption = EnumArchiveEncryption.AGE_X25519
    recipient = "age1testrecipient"

    def encrypt(self, data: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in data)

    def decrypt(self, data: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in data)


class MemoryWriter:
    def __init__(self) -> None:
        self.sent: list[
            tuple[str, bytes | None, bytes | None, list[tuple[str, bytes | None]], int]
        ] = []

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
        timestamp_ms: int,
    ) -> None:
        self.sent.append((topic, key, value, headers, timestamp_ms))


#: The first topic the archive contract declares (the tool-executed hook topic).
T = contract_source_topics()[0]


def source() -> FakeTopic:
    return FakeTopic(
        {
            T: [
                rec(T, 10, DAY1, b'{"a": 1}'),
                rec(T, 11, DAY1, b'{"a": 2}'),
                rec(T, 12, DAY2, b'{"a": 3}'),
            ]
        }
    )
