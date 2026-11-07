# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-publish dead-lettered records to the topic they came from (OMN-22811).

For each dead-letter entry that names its source record, the handler reads the
exact record from its source topic (group-less, commit-free) and publishes it
again to the same topic. It never resets or commits a consumer group's offset.

Safety, in the order the handler applies it:

* an entry with no source coordinates is skipped, never guessed;
* an entry whose source topic is itself a dead-letter topic is skipped, so a
  redrive cannot loop;
* an entry already recorded as redriven is skipped, so a rerun does not repeat;
* a source record retention removed is skipped and named;
* a dry run (the contract default) reads and lists and publishes nothing;
* the first publish failure stops the run; the entries after it stay
  NOT_ATTEMPTED and nothing is recorded for the failed one.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_dead_letter_redrive_effect.models import (
    EnumRedriveItemStatus,
    EnumRedriveVerdict,
    ModelDeadLetterRedriveConfig,
    ModelDeadLetterRedriveRequest,
    ModelDeadLetterRedriveResult,
    ModelDlqBacklogEntry,
    ModelRedriveItem,
    ModelSourceRecord,
)
from omnimarket.nodes.node_dead_letter_redrive_effect.protocols import (
    ProtocolDlqBacklog,
    ProtocolRecordPublisher,
    ProtocolRedriveLog,
    ProtocolSourceRecordReader,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


def contract_config() -> ModelDeadLetterRedriveConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelDeadLetterRedriveConfig.model_validate(
        data["config"]["dead_letter_redrive"]
    )


class MemoryDlqBacklog:
    """In-memory default: a fixed list of entries."""

    def __init__(self, entries: list[ModelDlqBacklogEntry] | None = None) -> None:
        self._entries = list(entries or [])

    def read_backlog(
        self, *, source_topic: str | None, limit: int
    ) -> list[ModelDlqBacklogEntry]:
        rows = [
            e
            for e in self._entries
            if source_topic is None or e.source_topic == source_topic
        ]
        return rows[:limit]


class MemorySourceRecordReader:
    """In-memory default: records keyed by (topic, partition, offset)."""

    def __init__(
        self, records: dict[tuple[str, int, int], ModelSourceRecord] | None = None
    ) -> None:
        self._records = dict(records or {})

    async def read_record(
        self, *, topic: str, partition: int, offset: int
    ) -> ModelSourceRecord | None:
        return self._records.get((topic, partition, offset))


class MemoryRecordPublisher:
    """In-memory default: keeps what was published."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(self, *, topic: str, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, key, value))


class MemoryRedriveLog:
    def __init__(self) -> None:
        self._done: set[str] = set()

    def has_redriven(self, entry_key: str) -> bool:
        return entry_key in self._done

    def record_redriven(self, entry_key: str) -> None:
        self._done.add(entry_key)


def _item(
    entry: ModelDlqBacklogEntry, status: EnumRedriveItemStatus, detail: str = ""
) -> ModelRedriveItem:
    return ModelRedriveItem(
        entry_key=entry.key,
        correlation_id=entry.correlation_id,
        source_topic=entry.source_topic,
        source_partition=entry.source_partition,
        source_offset=entry.source_offset,
        status=status,
        detail=detail,
    )


class HandlerDeadLetterRedrive:
    """Redrive through injected backlog, source reader, publisher and log."""

    def __init__(
        self,
        *,
        backlog: ProtocolDlqBacklog | None = None,
        source: ProtocolSourceRecordReader | None = None,
        publisher: ProtocolRecordPublisher | None = None,
        log: ProtocolRedriveLog | None = None,
        config: ModelDeadLetterRedriveConfig | None = None,
    ) -> None:
        self._backlog: ProtocolDlqBacklog = backlog or MemoryDlqBacklog()
        self._source: ProtocolSourceRecordReader = source or MemorySourceRecordReader()
        self._publisher: ProtocolRecordPublisher = publisher or MemoryRecordPublisher()
        self._log: ProtocolRedriveLog = log or MemoryRedriveLog()
        self._config = config or contract_config()

    async def handle(
        self, request: ModelDeadLetterRedriveRequest
    ) -> ModelDeadLetterRedriveResult:
        dry_run = self._config.dry_run if request.dry_run is None else request.dry_run
        limit = request.max_records or self._config.max_records
        entries = self._backlog.read_backlog(
            source_topic=request.source_topic, limit=limit
        )
        items: list[ModelRedriveItem] = []
        failed = False
        for entry in entries:
            if failed:
                items.append(_item(entry, EnumRedriveItemStatus.NOT_ATTEMPTED))
                continue
            item = await self._one(entry, dry_run=dry_run)
            items.append(item)
            failed = item.status is EnumRedriveItemStatus.FAILED
        return self._result(entries, items, dry_run=dry_run)

    async def _one(
        self, entry: ModelDlqBacklogEntry, *, dry_run: bool
    ) -> ModelRedriveItem:
        if (
            not entry.has_source
            or entry.source_topic is None
            or entry.source_partition is None
            or entry.source_offset is None
        ):
            return _item(entry, EnumRedriveItemStatus.SKIPPED_NO_SOURCE)
        if entry.source_topic.startswith(self._config.dlq_topic_prefix):
            return _item(entry, EnumRedriveItemStatus.SKIPPED_SOURCE_IS_DLQ)
        if self._log.has_redriven(entry.key):
            return _item(entry, EnumRedriveItemStatus.SKIPPED_ALREADY_REDRIVEN)
        record = await self._source.read_record(
            topic=entry.source_topic,
            partition=entry.source_partition,
            offset=entry.source_offset,
        )
        if record is None:
            return _item(entry, EnumRedriveItemStatus.SKIPPED_SOURCE_GONE)
        if dry_run:
            return _item(entry, EnumRedriveItemStatus.WOULD_REDRIVE)
        try:
            await self._publisher.publish(
                topic=entry.source_topic, key=record.key, value=record.value
            )
        except Exception as exc:
            return _item(
                entry, EnumRedriveItemStatus.FAILED, f"{type(exc).__name__}: {exc}"
            )
        self._log.record_redriven(entry.key)
        return _item(entry, EnumRedriveItemStatus.REDRIVEN)

    @staticmethod
    def _result(
        entries: list[ModelDlqBacklogEntry],
        items: list[ModelRedriveItem],
        *,
        dry_run: bool,
    ) -> ModelDeadLetterRedriveResult:
        def count(*statuses: EnumRedriveItemStatus) -> int:
            return sum(1 for i in items if i.status in statuses)

        redriven = count(EnumRedriveItemStatus.REDRIVEN)
        would = count(EnumRedriveItemStatus.WOULD_REDRIVE)
        failed = count(EnumRedriveItemStatus.FAILED)
        skipped = count(
            EnumRedriveItemStatus.SKIPPED_NO_SOURCE,
            EnumRedriveItemStatus.SKIPPED_SOURCE_IS_DLQ,
            EnumRedriveItemStatus.SKIPPED_SOURCE_GONE,
            EnumRedriveItemStatus.SKIPPED_ALREADY_REDRIVEN,
        )
        if failed:
            verdict = EnumRedriveVerdict.PARTIAL
        elif dry_run:
            verdict = EnumRedriveVerdict.DRY_RUN
        elif redriven:
            verdict = EnumRedriveVerdict.REDRIVEN
        else:
            verdict = EnumRedriveVerdict.NOTHING_TO_REDRIVE
        with_source = sum(1 for e in entries if e.has_source)
        return ModelDeadLetterRedriveResult(
            verdict=verdict,
            dry_run=dry_run,
            backlog_read=len(entries),
            with_source=with_source,
            without_source=len(entries) - with_source,
            redriven=redriven,
            would_redrive=would,
            skipped=skipped,
            failed=failed,
            items=items,
        )
