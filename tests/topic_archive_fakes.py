# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# The KMS and S3 fakes take boto3's own PascalCase keyword names.
# ruff: noqa: N803
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


KMS_KEY_ARN = "arn:aws:kms:us-east-1:000000000000:key/test-key"


class FakeKms:
    """Stands in for a boto3 KMS client: wraps a data key by XOR, checks context."""

    def __init__(self) -> None:
        self.generated = 0
        self.decrypted = 0

    @staticmethod
    def _wrap(key: bytes, context: dict[str, str]) -> bytes:
        tag = repr(sorted(context.items())).encode()
        return b"wrapped|" + tag + b"|" + bytes(b ^ 0xA5 for b in key)

    def generate_data_key(
        self, *, KeyId: str, KeySpec: str, EncryptionContext: dict[str, str]
    ) -> dict[str, object]:
        assert KeySpec == "AES_256"
        self.generated += 1
        key = bytes(range(self.generated, self.generated + 32))
        return {
            "Plaintext": key,
            "CiphertextBlob": self._wrap(key, EncryptionContext),
            "KeyId": KMS_KEY_ARN,
        }

    def decrypt(
        self, *, CiphertextBlob: bytes, EncryptionContext: dict[str, str], KeyId: str
    ) -> dict[str, object]:
        tag = repr(sorted(EncryptionContext.items())).encode()
        prefix = b"wrapped|" + tag + b"|"
        if not CiphertextBlob.startswith(prefix) or KeyId != KMS_KEY_ARN:
            raise PermissionError("InvalidCiphertextException")
        self.decrypted += 1
        body = CiphertextBlob[len(prefix) :]
        return {"Plaintext": bytes(b ^ 0xA5 for b in body), "KeyId": KMS_KEY_ARN}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _Paginator:
    def __init__(self, store: dict[str, dict[str, object]]) -> None:
        self._store = store

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, object]]:
        keys = sorted(k for k in self._store if k.startswith(f"{Bucket}/{Prefix}"))
        # two pages, so the sink must follow pagination
        half = len(keys) // 2
        return [
            {"Contents": [{"Key": k.split("/", 1)[1]} for k in part]} if part else {}
            for part in (keys[:half], keys[half:])
        ]


class FakeS3:
    """Stands in for a boto3 S3 client over an in-memory bucket."""

    def __init__(self, *, report_key: str | None = None) -> None:
        self.store: dict[str, dict[str, object]] = {}
        self._report_key = report_key

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ServerSideEncryption: str,
        SSEKMSKeyId: str,
    ) -> None:
        self.store[f"{Bucket}/{Key}"] = {
            "Body": Body,
            "ServerSideEncryption": ServerSideEncryption,
            "SSEKMSKeyId": self._report_key or SSEKMSKeyId,
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        o = self.store[f"{Bucket}/{Key}"]
        body = o["Body"]
        assert isinstance(body, bytes)
        return {
            "ServerSideEncryption": o["ServerSideEncryption"],
            "SSEKMSKeyId": o["SSEKMSKeyId"],
            "ContentLength": len(body),
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        body = self.store[f"{Bucket}/{Key}"]["Body"]
        assert isinstance(body, bytes)
        return {"Body": _Body(body)}

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self.store)
