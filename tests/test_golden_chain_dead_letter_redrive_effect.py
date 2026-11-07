# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for node_dead_letter_redrive_effect (OMN-22811).

Golden: a dead letter that names its source and whose record retention still
holds is published once to its source topic, and a rerun publishes nothing.
Error: each refusal and the publish failure leave the run in the state the
contract says.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_dead_letter_redrive_effect.handlers.handler_dead_letter_redrive import (
    HandlerDeadLetterRedrive,
    MemoryDlqBacklog,
    MemoryRecordPublisher,
    MemoryRedriveLog,
    MemorySourceRecordReader,
    contract_config,
)
from omnimarket.nodes.node_dead_letter_redrive_effect.handlers.live import (
    JsonlRedriveLog,
    build_live_handler,
    entry_from_ledger_row,
)
from omnimarket.nodes.node_dead_letter_redrive_effect.models import (
    EnumRedriveItemStatus,
    EnumRedriveVerdict,
    ModelDeadLetterRedriveRequest,
    ModelDlqBacklogEntry,
    ModelSourceRecord,
)

pytestmark = pytest.mark.unit

SRC = "onex.evt.omnimarket.delegation-completed.v1"
DLQ = "onex.dlq.omnimarket.projection.v1"


def entry(
    dlq_offset: int,
    *,
    source_topic: str | None = SRC,
    source_partition: int | None = 3,
    source_offset: int | None = 100,
) -> ModelDlqBacklogEntry:
    return ModelDlqBacklogEntry(
        dlq_topic=DLQ,
        dlq_partition=0,
        dlq_offset=dlq_offset,
        correlation_id=f"c-{dlq_offset}",
        failure_reason="tenant not in mirror",
        handler="projection-delegation",
        source_topic=source_topic,
        source_partition=source_partition,
        source_offset=source_offset,
    )


RECORD = ModelSourceRecord(key=b"k", value=b'{"a":1}')


class FailingPublisher(MemoryRecordPublisher):
    async def publish(self, *, topic: str, key: bytes | None, value: bytes) -> None:
        raise ConnectionError("broker unreachable")


def build(
    entries: list[ModelDlqBacklogEntry],
    *,
    records: dict[tuple[str, int, int], ModelSourceRecord] | None = None,
    publisher: MemoryRecordPublisher | None = None,
    log: Any = None,
) -> tuple[HandlerDeadLetterRedrive, MemoryRecordPublisher]:
    pub = publisher or MemoryRecordPublisher()
    handler = HandlerDeadLetterRedrive(
        backlog=MemoryDlqBacklog(entries),
        source=MemorySourceRecordReader(
            records if records is not None else {(SRC, 3, 100): RECORD}
        ),
        publisher=pub,
        log=log or MemoryRedriveLog(),
    )
    return handler, pub


LIVE = ModelDeadLetterRedriveRequest(dry_run=False)


async def test_golden_a_retained_source_record_is_republished_once() -> None:
    handler, pub = build([entry(1)])
    first = await handler.handle(LIVE)
    assert first.verdict is EnumRedriveVerdict.REDRIVEN
    assert first.redriven == 1
    assert pub.published == [(SRC, b"k", b'{"a":1}')]
    again = await handler.handle(LIVE)
    assert again.verdict is EnumRedriveVerdict.NOTHING_TO_REDRIVE
    assert again.items[0].status is EnumRedriveItemStatus.SKIPPED_ALREADY_REDRIVEN
    assert len(pub.published) == 1


async def test_golden_dry_run_is_the_contract_default_and_publishes_nothing() -> None:
    assert contract_config().dry_run is True
    handler, pub = build([entry(1)])
    result = await handler.handle(ModelDeadLetterRedriveRequest())
    assert result.verdict is EnumRedriveVerdict.DRY_RUN
    assert result.would_redrive == 1
    assert pub.published == []


async def test_error_entries_without_a_usable_source_are_skipped_and_named() -> None:
    entries = [
        entry(1, source_topic=None, source_partition=None, source_offset=None),
        entry(2, source_topic=DLQ),
        entry(3, source_offset=999),
    ]
    handler, pub = build(entries)
    result = await handler.handle(LIVE)
    assert [i.status for i in result.items] == [
        EnumRedriveItemStatus.SKIPPED_NO_SOURCE,
        EnumRedriveItemStatus.SKIPPED_SOURCE_IS_DLQ,
        EnumRedriveItemStatus.SKIPPED_SOURCE_GONE,
    ]
    assert result.skipped == 3
    assert result.without_source == 1
    assert pub.published == []


async def test_error_publish_failure_stops_the_run_and_records_nothing() -> None:
    log = MemoryRedriveLog()
    handler, _ = build(
        [entry(1), entry(2, source_offset=101)],
        records={(SRC, 3, 100): RECORD, (SRC, 3, 101): RECORD},
        publisher=FailingPublisher(),
        log=log,
    )
    result = await handler.handle(LIVE)
    assert result.verdict is EnumRedriveVerdict.PARTIAL
    assert [i.status for i in result.items] == [
        EnumRedriveItemStatus.FAILED,
        EnumRedriveItemStatus.NOT_ATTEMPTED,
    ]
    assert "broker unreachable" in result.items[0].detail
    assert not log.has_redriven(entry(1).key)


async def test_max_records_bounds_a_run() -> None:
    handler, pub = build([entry(i, source_offset=100) for i in range(1, 6)])
    result = await handler.handle(
        ModelDeadLetterRedriveRequest(dry_run=False, max_records=2)
    )
    assert result.backlog_read == 2
    assert len(pub.published) == 2


async def test_source_topic_filter_reads_only_that_topic() -> None:
    other = entry(2, source_topic="onex.evt.other.v1")
    handler, _ = build([entry(1), other])
    result = await handler.handle(
        ModelDeadLetterRedriveRequest(dry_run=True, source_topic=SRC)
    )
    assert [i.entry_key for i in result.items] == [entry(1).key]


def test_jsonl_log_survives_a_restart(tmp_path: Any) -> None:
    path = tmp_path / "log" / "redriven.jsonl"
    JsonlRedriveLog(path).record_redriven("k1")
    assert JsonlRedriveLog(path).has_redriven("k1")
    assert not JsonlRedriveLog(path).has_redriven("k2")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_ledger_row_with_coordinates_and_without() -> None:
    env = {
        "correlation_id": "c-1",
        "failure_reason": "r",
        "handler": "h",
        "source_topic": SRC,
        "source_partition": 3,
        "source_offset": 100,
    }
    got = entry_from_ledger_row(
        topic=DLQ, partition=0, offset=9, value=json.dumps(env).encode()
    )
    assert got.has_source
    assert got.source_offset == 100
    bare = entry_from_ledger_row(topic=DLQ, partition=0, offset=10, value=b"not json")
    assert not bare.has_source


def test_live_binding_refuses_before_any_read_when_unset() -> None:
    config = contract_config()
    with pytest.raises(ValueError, match="binding is missing"):
        build_live_handler(config, {})
    named = config.model_copy(
        update={
            "binding": {
                "database_url_env": "DB_ENV",
                "bootstrap_servers_env": "BS_ENV",
                "redrive_log_path": "/tmp/x.jsonl",
            }
        }
    )
    with pytest.raises(ValueError, match="are unset"):
        build_live_handler(named, {})


async def test_control_a_log_that_forgets_republishes_the_same_record() -> None:
    """The failing control for the idempotence reading: with a log that never
    remembers, the rerun publishes again, which test_golden_a_retained_source_
    record_is_republished_once refuses."""

    class ForgetfulLog:
        def has_redriven(self, entry_key: str) -> bool:
            return False

        def record_redriven(self, entry_key: str) -> None:
            return None

    handler, pub = build([entry(1)], log=ForgetfulLog())
    await handler.handle(LIVE)
    await handler.handle(LIVE)
    assert len(pub.published) == 2
