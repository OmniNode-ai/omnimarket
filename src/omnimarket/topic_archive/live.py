# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live implementations of the archive boundary.

* ``AiokafkaTopicReader`` reads by partition assignment with no consumer group,
  so it commits nothing and changes no group state on the broker.
* ``LocalDirArchiveSink`` stages archives in a local directory, owner-only.
* ``AgeArchiveCipher`` encrypts to an age X25519 recipient. The archiver needs
  only the public recipient; decryption needs the identity, which only the
  replay side is given.
* ``AiokafkaReplayWriter`` produces replayed records.

Broker auth comes from the shared omnibase_infra builder over the lane's
standard KAFKA_* environment, the same one every runtime client uses, so no
credential is read or named here.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

from omnimarket.topic_archive.models import EnumArchiveEncryption, ModelArchivedRecord
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
    ProtocolReplayWriter,
    ProtocolTopicReader,
)

_FETCH_TIMEOUT_MS = 5000
_EMPTY_POLLS_BEFORE_GIVING_UP = 6


class NoArchiveCipher:
    """Identity transform. Refused by any sink that requires encryption."""

    encryption = EnumArchiveEncryption.NONE
    recipient: str | None = None

    def encrypt(self, data: bytes) -> bytes:
        return data

    def decrypt(self, data: bytes) -> bytes:
        return data


class AgeArchiveCipher:
    """age (X25519) encryption; ``identity`` is only needed to decrypt."""

    encryption = EnumArchiveEncryption.AGE_X25519

    def __init__(self, *, recipient: str, identity: str | None = None) -> None:
        import pyrage

        self.recipient: str | None = recipient
        self._recipient = pyrage.x25519.Recipient.from_str(recipient)
        self._identity = pyrage.x25519.Identity.from_str(identity) if identity else None

    def encrypt(self, data: bytes) -> bytes:
        import pyrage

        out: bytes = pyrage.encrypt(data, [self._recipient])
        return out

    def decrypt(self, data: bytes) -> bytes:
        import pyrage

        if self._identity is None:
            raise RuntimeError(
                "decryption needs the age identity; this cipher holds only the recipient"
            )
        out: bytes = pyrage.decrypt(data, [self._identity])
        return out


class LocalDirArchiveSink:
    """A local staging directory: dirs 0700, files 0600, atomic writes."""

    requires_encryption = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.location = f"file://{root}"

    def _path(self, name: str) -> Path:
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(
                f"archive object name must be relative and inside the sink: {name!r}"
            )
        return self.root.joinpath(*rel.parts)

    def put(self, name: str, data: bytes) -> None:
        path = self._path(name)
        self._owner_only_dirs(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _owner_only_dirs(self, directory: Path) -> None:
        """Create the sink root and every directory under it with mode 0700.

        Path.mkdir(parents=True, mode=...) applies the mode to the last
        directory only, so the root and intermediate levels would otherwise
        take the process umask.
        """
        chain = [directory, *directory.parents]
        stop = chain.index(self.root) + 1 if self.root in chain else len(chain)
        for d in reversed(chain[:stop]):
            if not d.exists():
                d.mkdir(mode=0o700)
                os.chmod(d, 0o700)

    def get(self, name: str) -> bytes:
        return self._path(name).read_bytes()

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def list_names(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        names = (
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.startswith(".tmp-")
        )
        return sorted(n for n in names if n.startswith(prefix))


class AiokafkaTopicReader:
    """Group-less, commit-free reader over explicit partition assignment."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._consumer: Any = None

    async def _client(self) -> Any:
        if self._consumer is None:
            from aiokafka import AIOKafkaConsumer
            from omnibase_infra.event_bus.kafka_auth import (
                build_aiokafka_auth_kwargs_from_env,
            )

            self._consumer = AIOKafkaConsumer(
                bootstrap_servers=self._bootstrap,
                group_id=None,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                **build_aiokafka_auth_kwargs_from_env(),
            )
            await self._consumer.start()
        return self._consumer

    async def close(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    async def partitions(self, topic: str) -> list[int]:
        # The consumer's partitions_for_topic() answers from metadata it
        # already holds and returns None for a topic it has never been
        # assigned, which read as "no partitions" and archived nothing in the
        # first live run. Ask the broker through the admin API instead.
        from aiokafka.admin import AIOKafkaAdminClient
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        await self._client()  # resolves the bootstrap address
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._bootstrap,
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await admin.start()
        try:
            described = await admin.describe_topics([topic])
        finally:
            await admin.close()
        found: list[int] = []
        for entry in described:
            if entry.get("topic") == topic and not entry.get("error_code"):
                found.extend(int(p["partition"]) for p in entry.get("partitions", []))
        return sorted(found)

    async def watermarks(self, topic: str, partition: int) -> tuple[int, int]:
        from aiokafka import TopicPartition

        consumer = await self._client()
        tp = TopicPartition(topic, partition)
        low = (await consumer.beginning_offsets([tp]))[tp]
        high = (await consumer.end_offsets([tp]))[tp]
        return int(low), int(high)

    async def read_range(
        self, topic: str, partition: int, start: int, end_exclusive: int
    ) -> AsyncIterator[ModelArchivedRecord]:
        from aiokafka import TopicPartition

        consumer = await self._client()
        tp = TopicPartition(topic, partition)
        consumer.assign([tp])
        low, _high = await self.watermarks(topic, partition)
        consumer.seek(tp, max(start, low))
        empty = 0
        while await consumer.position(tp) < end_exclusive:
            batch = await consumer.getmany(
                tp, timeout_ms=_FETCH_TIMEOUT_MS, max_records=2000
            )
            msgs = batch.get(tp, [])
            if not msgs:
                empty += 1
                if empty >= _EMPTY_POLLS_BEFORE_GIVING_UP:
                    return
                continue
            empty = 0
            for m in msgs:
                if m.offset >= end_exclusive:
                    return
                yield ModelArchivedRecord.from_bytes(
                    topic=topic,
                    partition=partition,
                    offset=m.offset,
                    timestamp_ms=m.timestamp,
                    key=m.key,
                    value=m.value,
                    headers=[(k, v) for k, v in (m.headers or ())],
                )


class AiokafkaReplayWriter:
    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
        timestamp_ms: int,
    ) -> None:
        if self._producer is None:
            from aiokafka import AIOKafkaProducer
            from omnibase_infra.event_bus.kafka_auth import (
                build_aiokafka_auth_kwargs_from_env,
            )

            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap,
                acks="all",
                **build_aiokafka_auth_kwargs_from_env(),
            )
            await self._producer.start()
        await self._producer.send_and_wait(
            topic,
            key=key,
            value=value,
            headers=[(k, v if v is not None else b"") for k, v in headers],
            timestamp_ms=timestamp_ms,
        )

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


#: Bootstrap-only addressing for runtime dispatch. The broker address and its
#: auth come from the lane's standard KAFKA_* environment; the staging
#: directory and the age recipient/identity are read when first used.
STAGING_DIR_ENV = "ONEX_TOPIC_ARCHIVE_STAGING_DIR"
AGE_RECIPIENT_ENV = "ONEX_TOPIC_ARCHIVE_AGE_RECIPIENT"
AGE_IDENTITY_ENV = "ONEX_TOPIC_ARCHIVE_AGE_IDENTITY"
BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"


class _LazySink:
    """A LocalDirArchiveSink whose directory is resolved on first use."""

    requires_encryption = False

    def _sink(self) -> LocalDirArchiveSink:
        return LocalDirArchiveSink(Path(os.environ[STAGING_DIR_ENV]))

    @property
    def location(self) -> str:
        return self._sink().location

    def put(self, name: str, data: bytes) -> None:
        self._sink().put(name, data)

    def get(self, name: str) -> bytes:
        return self._sink().get(name)

    def exists(self, name: str) -> bool:
        return self._sink().exists(name)

    def list_names(self, prefix: str) -> list[str]:
        return self._sink().list_names(prefix)


def _cipher_from_env(*, with_identity: bool) -> NoArchiveCipher | AgeArchiveCipher:
    recipient = os.environ.get(AGE_RECIPIENT_ENV)
    if not recipient:
        return NoArchiveCipher()
    identity = os.environ[AGE_IDENTITY_ENV] if with_identity else None
    return AgeArchiveCipher(recipient=recipient, identity=identity)


class _LazyReader(AiokafkaTopicReader):
    def __init__(self) -> None:
        super().__init__("")

    async def _client(self) -> Any:
        if not self._bootstrap:
            self._bootstrap = os.environ[BOOTSTRAP_ENV]
        return await super()._client()


class _LazyWriter(AiokafkaReplayWriter):
    def __init__(self) -> None:
        super().__init__("")

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
        timestamp_ms: int,
    ) -> None:
        if not self._bootstrap:
            self._bootstrap = os.environ[BOOTSTRAP_ENV]
        await super().publish(
            topic, key=key, value=value, headers=headers, timestamp_ms=timestamp_ms
        )


class _LazyCipher:
    """Resolves the cipher from the environment on first use."""

    def __init__(self, *, with_identity: bool) -> None:
        self._with_identity = with_identity

    def _c(self) -> NoArchiveCipher | AgeArchiveCipher:
        return _cipher_from_env(with_identity=self._with_identity)

    @property
    def encryption(self) -> EnumArchiveEncryption:
        return self._c().encryption

    @property
    def recipient(self) -> str | None:
        return self._c().recipient

    def encrypt(self, data: bytes) -> bytes:
        return self._c().encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        return self._c().decrypt(data)


def live_archive_boundary() -> tuple[
    ProtocolTopicReader, ProtocolArchiveSink, ProtocolArchiveCipher
]:
    return _LazyReader(), _LazySink(), _LazyCipher(with_identity=False)


def live_replay_boundary() -> tuple[
    ProtocolArchiveSink, ProtocolArchiveCipher, ProtocolReplayWriter
]:
    return _LazySink(), _LazyCipher(with_identity=True), _LazyWriter()
