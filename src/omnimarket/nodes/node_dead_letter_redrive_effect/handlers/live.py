# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-backend binding of the dead-letter redrive (OMN-22811).

* backlog: a read-only select over ``event_ledger`` dead-letter rows (psycopg2);
* source: the topic archive's group-less, commit-free aiokafka reader;
* publisher: an aiokafka producer on the lane's standard KAFKA_* environment,
  with the broker auth from the shared omnibase_infra builder;
* log: an owner-only JSON Lines file of redriven entry keys.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_dead_letter_redrive_effect.handlers.handler_dead_letter_redrive import (
    HandlerDeadLetterRedrive,
)
from omnimarket.nodes.node_dead_letter_redrive_effect.models import (
    ModelDeadLetterRedriveConfig,
    ModelDlqBacklogEntry,
    ModelSourceRecord,
)
from omnimarket.nodes.node_topic_archive_effect.handlers._kafka_topic_reader import (
    AiokafkaTopicReader,
)

_FETCH_SIZE = 500


def entry_from_ledger_row(
    *, topic: str, partition: int, offset: int, value: bytes
) -> ModelDlqBacklogEntry:
    """Build one backlog entry from an event_ledger row's raw envelope bytes.

    An envelope that is not JSON, or carries no source coordinates, still
    becomes an entry, with no source, so the handler counts and skips it.
    """
    try:
        envelope: Any = json.loads(value)
    except (ValueError, UnicodeDecodeError):
        envelope = {}
    if not isinstance(envelope, dict):
        envelope = {}

    def _int(name: str) -> int | None:
        raw = envelope.get(name)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else None

    def _str(name: str) -> str | None:
        raw = envelope.get(name)
        return raw if isinstance(raw, str) else None

    return ModelDlqBacklogEntry(
        dlq_topic=topic,
        dlq_partition=partition,
        dlq_offset=offset,
        correlation_id=_str("correlation_id"),
        failure_reason=_str("failure_reason"),
        handler=_str("handler"),
        source_topic=_str("source_topic"),
        source_partition=_int("source_partition"),
        source_offset=_int("source_offset"),
    )


class PostgresDlqBacklog:
    """Read-only: selects dead-letter rows and never writes."""

    def __init__(self, dsn: str, *, topic_like: str) -> None:
        self._dsn = dsn
        self._topic_like = topic_like

    def read_backlog(
        self, *, source_topic: str | None, limit: int
    ) -> list[ModelDlqBacklogEntry]:
        """Oldest first. The source-topic filter is applied here, not in SQL, so a
        poison row whose value is not JSON cannot fail the whole select."""
        psycopg2: Any = importlib.import_module("psycopg2")  # untyped package

        out: list[ModelDlqBacklogEntry] = []
        conn = psycopg2.connect(self._dsn)
        try:
            conn.set_session(readonly=True)
            with conn.cursor(name="dlq_redrive_backlog") as cur:  # server-side
                cur.itersize = _FETCH_SIZE
                cur.execute(
                    "select topic, partition, kafka_offset, event_value "
                    "from event_ledger where topic like %s "
                    "order by coalesce(event_timestamp, ledger_written_at), kafka_offset",
                    (self._topic_like,),
                )
                for r in cur:
                    got = entry_from_ledger_row(
                        topic=r[0], partition=r[1], offset=r[2], value=bytes(r[3])
                    )
                    if source_topic is not None and got.source_topic != source_topic:
                        continue
                    out.append(got)
                    if len(out) >= limit:
                        break
        finally:
            conn.close()
        return out


class AiokafkaSourceRecordReader:
    """One record by coordinates, through the commit-free archive reader."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._reader = AiokafkaTopicReader(bootstrap_servers)

    async def close(self) -> None:
        await self._reader.close()

    async def read_record(
        self, *, topic: str, partition: int, offset: int
    ) -> ModelSourceRecord | None:
        low, high = await self._reader.watermarks(topic, partition)
        if offset < low or offset >= high:
            return None
        async for rec in self._reader.read_range(topic, partition, offset, offset + 1):
            if rec.offset == offset:
                value = rec.value_bytes()
                if value is None:
                    return None  # a tombstone carries nothing to redrive
                return ModelSourceRecord(key=rec.key_bytes(), value=value)
        return None


class AiokafkaRecordPublisher:
    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def _client(self) -> Any:
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
        return self._producer

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, *, topic: str, key: bytes | None, value: bytes) -> None:
        producer = await self._client()
        await producer.send_and_wait(topic, value=value, key=key)


class JsonlRedriveLog:
    """Owner-only JSON Lines file of redriven entry keys."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._done: set[str] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._done.add(json.loads(line)["entry_key"])

    def has_redriven(self, entry_key: str) -> bool:
        return entry_key in self._done

    def record_redriven(self, entry_key: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"entry_key": entry_key}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._done.add(entry_key)


def build_live_handler(
    config: ModelDeadLetterRedriveConfig, environ: Mapping[str, str]
) -> HandlerDeadLetterRedrive:
    """The real-backend handler, bound by the names the contract's overlay holds.

    ``binding`` names the environment variables to read, never their values:
    ``database_url_env`` and ``bootstrap_servers_env``, plus ``redrive_log_path``.
    A missing name or an unset variable raises before any read or publish.
    """
    binding = config.binding
    names = {
        k: binding.get(k)
        for k in ("database_url_env", "bootstrap_servers_env", "redrive_log_path")
    }
    missing = [k for k, v in names.items() if not v]
    if missing:
        raise ValueError(f"dead_letter_redrive.binding is missing: {sorted(missing)}")
    dsn = environ.get(str(names["database_url_env"]))
    bootstrap = environ.get(str(names["bootstrap_servers_env"]))
    if not dsn or not bootstrap:
        raise ValueError(
            "the environment variables named by dead_letter_redrive.binding are unset"
        )
    return HandlerDeadLetterRedrive(
        backlog=PostgresDlqBacklog(dsn, topic_like=config.dlq_topic_like),
        source=AiokafkaSourceRecordReader(bootstrap),
        publisher=AiokafkaRecordPublisher(bootstrap),
        log=JsonlRedriveLog(Path(str(names["redrive_log_path"]))),
        config=config,
    )
