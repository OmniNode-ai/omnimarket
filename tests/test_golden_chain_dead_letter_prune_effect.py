# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dead_letter_prune_effect: archive, verify, then prune (OMN-17001).

The invariant under test is the one the operator's consent names: no row is
deleted unless the archive object holding it was read back from the sink,
decrypted, checksummed and recounted, and the source window still holds exactly
the offsets the archive holds.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dead_letter_prune_effect.handlers.handler_dead_letter_prune import (
    HandlerDeadLetterPrune,
    contract_config,
)
from omnimarket.nodes.node_dead_letter_prune_effect.models import (
    EnumDeadLetterDayStatus,
    EnumDeadLetterPruneVerdict,
    ModelDeadLetterDay,
    ModelDeadLetterPruneRequest,
    ModelDeadLetterRow,
    ModelDeadLetterWindowManifest,
)
from omnimarket.topic_archive.live import NoArchiveCipher
from tests.topic_archive_fakes import MemorySink, XorCipher

pytestmark = pytest.mark.unit

DLQ = "onex.dlq.omnibase-infra.events.v1"
AS_OF = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)
OLD = dt.date(2026, 8, 21)  # 35 days before AS_OF
EDGE = dt.date(2026, 8, 25)  # 31 days before AS_OF: whole day older than 30 days
NEW = dt.date(2026, 8, 26)  # the cutoff day itself is kept


def row(
    offset: int, day: dt.date, *, topic: str = DLQ, value: bytes = b"{}"
) -> ModelDeadLetterRow:
    ts = dt.datetime.combine(day, dt.time(10, 0), tzinfo=dt.UTC) + dt.timedelta(
        seconds=offset
    )
    return ModelDeadLetterRow.from_values(
        ledger_entry_id=f"00000000-0000-0000-0000-{offset:012d}",
        topic=topic,
        partition=0,
        kafka_offset=offset,
        event_key=None,
        event_value=value,
        onex_headers={"event_type": "dlq_raw_message"},
        envelope_id=None,
        correlation_id=None,
        event_type="dlq_raw_message",
        source="local",
        event_timestamp=ts,
        ledger_written_at=ts,
    )


class FakeStore:
    """event_ledger in memory, keyed on (topic, partition, kafka_offset)."""

    def __init__(self, rows: list[ModelDeadLetterRow]) -> None:
        self.rows = {(r.topic, r.partition, r.kafka_offset): r for r in rows}
        self.read_days: list[dt.date] = []
        self.deleted: list[int] = []
        self.delete_calls = 0
        self.drop_on_delete: int | None = None  # simulate a concurrent delete

    @staticmethod
    def _day(r: ModelDeadLetterRow) -> dt.date:
        return r.day_basis().date()

    def list_days(
        self, *, cutoff: dt.date, topic_like: str
    ) -> list[ModelDeadLetterDay]:
        prefix = topic_like.rstrip("%")
        counts: dict[tuple[str, int, dt.date], int] = {}
        for r in self.rows.values():
            if r.topic.startswith(prefix) and self._day(r) < cutoff:
                k = (r.topic, r.partition, self._day(r))
                counts[k] = counts.get(k, 0) + 1
        return [
            ModelDeadLetterDay(topic=t, partition=p, day=d, row_count=n)
            for (t, p, d), n in sorted(
                counts.items(), key=lambda kv: (kv[0][2], kv[0][0])
            )
        ]

    def _window(
        self, topic: str, partition: int, day: dt.date
    ) -> list[ModelDeadLetterRow]:
        return sorted(
            (
                r
                for r in self.rows.values()
                if r.topic == topic and r.partition == partition and self._day(r) == day
            ),
            key=lambda r: r.kafka_offset,
        )

    def read_chunk(
        self, *, topic: str, partition: int, day: dt.date, after_offset: int, limit: int
    ) -> list[ModelDeadLetterRow]:
        self.read_days.append(day)
        return [
            r
            for r in self._window(topic, partition, day)
            if r.kafka_offset > after_offset
        ][:limit]

    def window_offsets(
        self,
        *,
        topic: str,
        partition: int,
        day: dt.date,
        first_offset: int,
        last_offset: int,
    ) -> list[int]:
        return [
            r.kafka_offset
            for r in self._window(topic, partition, day)
            if first_offset <= r.kafka_offset <= last_offset
        ]

    def delete_offsets(self, *, topic: str, partition: int, offsets: list[int]) -> int:
        self.delete_calls += 1
        n = 0
        for o in offsets:
            if self.drop_on_delete == o:
                continue
            if self.rows.pop((topic, partition, o), None) is not None:
                n += 1
                self.deleted.append(o)
        return n


class CorruptingSink(MemorySink):
    def get(self, name: str) -> bytes:
        data = super().get(name)
        if name.endswith(".manifest.json"):
            return data
        return data[:-1] + bytes([data[-1] ^ 0xFF])


def handler(
    store: FakeStore, sink: MemorySink | None = None, cipher: object | None = None
) -> HandlerDeadLetterPrune:
    return HandlerDeadLetterPrune(
        store=store,
        sink=sink if sink is not None else MemorySink(),
        cipher=cipher if cipher is not None else XorCipher(),  # type: ignore[arg-type]
        now=AS_OF,
        max_rows_per_object=2,
        delete_batch_size=2,
    )


def seed() -> FakeStore:
    rows = [row(o, OLD) for o in (10, 11, 12)]
    rows += [row(20, EDGE)]
    rows += [row(30, NEW), row(31, NEW)]
    rows += [row(40, OLD, topic="onex.evt.omniclaude.tool-executed.v1")]
    return FakeStore(rows)


# --- contract ---------------------------------------------------------------


def test_contract_declares_thirty_day_retention_on_event_ledger_dead_letter_topics() -> (
    None
):
    cfg = contract_config()
    assert cfg.retention_days == 30
    assert cfg.table == "event_ledger"
    assert cfg.topic_like == "onex.dlq.%"
    assert cfg.dsn_env == "OMNIBASE_INFRA_DB_URL"
    raw = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_dead_letter_prune_effect/contract.yaml"
        ).read_text()
    )
    assert raw["node_type"] == "EFFECT_GENERIC"
    assert (
        raw["config"]["dead_letter_prune"]["sinks"]["s3"]["requires_encryption"] is True
    )


# --- the happy path -----------------------------------------------------------


def test_archives_then_prunes_only_whole_days_older_than_retention() -> None:
    store = seed()
    sink = MemorySink()
    result = handler(store, sink).handle(ModelDeadLetterPruneRequest())

    assert result.verdict == EnumDeadLetterPruneVerdict.PRUNED
    assert result.cutoff_day == dt.date(2026, 8, 26)
    assert sorted(store.deleted) == [10, 11, 12, 20]
    # rows on the cutoff day and a non-dead-letter topic are never touched
    assert {k[2] for k in store.rows} == {30, 31, 40}
    assert NEW not in store.read_days
    assert result.rows_pruned == 4
    assert all(d.status == EnumDeadLetterDayStatus.PRUNED for d in result.days)


def test_each_object_round_trips_byte_exact_with_a_manifest() -> None:
    store = seed()
    sink = MemorySink()
    original = {o: store.rows[(DLQ, 0, o)] for o in (10, 11, 12)}
    handler(store, sink).handle(ModelDeadLetterPruneRequest())

    manifests = [n for n in sink.objects if n.endswith(".manifest.json")]
    # 3 rows on OLD at 2 rows per object -> 2 objects; 1 on EDGE -> 1 object
    assert len(manifests) == 3
    back: dict[int, ModelDeadLetterRow] = {}
    for m_name in manifests:
        m = ModelDeadLetterWindowManifest.model_validate_json(sink.objects[m_name])
        plain = gzip.decompress(XorCipher().decrypt(sink.objects[m.object_name]))
        lines = [json.loads(line) for line in plain.splitlines()]
        assert len(lines) == m.record_count
        for obj in lines:
            r = ModelDeadLetterRow.model_validate(obj)
            back[r.kafka_offset] = r
    for o, r in original.items():
        assert back[o] == r
        assert back[o].value_bytes() == b"{}"


def test_non_utf8_values_survive_the_archive() -> None:
    store = FakeStore([row(1, OLD, value=b"\x00\xffnot-utf8")])
    sink = MemorySink()
    handler(store, sink).handle(ModelDeadLetterPruneRequest())
    (obj,) = [n for n in sink.objects if n.endswith(".gz.age")]
    plain = gzip.decompress(XorCipher().decrypt(sink.objects[obj]))
    assert ModelDeadLetterRow.model_validate_json(
        plain.splitlines()[0]
    ).value_bytes() == (b"\x00\xffnot-utf8")


# --- never prune an unverified row -------------------------------------------


def test_dry_run_reads_nothing_writes_nothing_deletes_nothing() -> None:
    store = seed()
    sink = MemorySink()
    result = handler(store, sink).handle(ModelDeadLetterPruneRequest(dry_run=True))
    assert result.verdict == EnumDeadLetterPruneVerdict.DRY_RUN
    assert sink.objects == {}
    assert store.deleted == []
    assert store.read_days == []
    assert result.rows_eligible == 4
    assert all(d.status == EnumDeadLetterDayStatus.DRY_RUN for d in result.days)


def test_a_corrupted_readback_fails_the_day_and_deletes_nothing() -> None:
    store = seed()
    result = handler(store, CorruptingSink()).handle(ModelDeadLetterPruneRequest())
    assert result.verdict == EnumDeadLetterPruneVerdict.FAILED
    assert store.deleted == []
    assert all(d.status == EnumDeadLetterDayStatus.FAILED for d in result.days)


def test_source_drift_between_archive_and_prune_refuses_the_delete() -> None:
    store = seed()

    class GrowingStore(FakeStore):
        def window_offsets(self, **kw: object) -> list[int]:
            offsets = super().window_offsets(**kw)  # type: ignore[arg-type]
            return [*offsets, 999] if offsets else offsets

    grown = GrowingStore(list(store.rows.values()))
    result = handler(grown).handle(ModelDeadLetterPruneRequest())
    assert result.verdict == EnumDeadLetterPruneVerdict.FAILED
    assert grown.deleted == []


def test_a_short_delete_is_reported_failed() -> None:
    store = seed()
    store.drop_on_delete = 11
    result = handler(store).handle(ModelDeadLetterPruneRequest())
    assert result.verdict == EnumDeadLetterPruneVerdict.FAILED
    day = next(d for d in result.days if d.day == OLD)
    assert day.status == EnumDeadLetterDayStatus.FAILED
    assert "deleted 1 of 2" in day.detail


def test_a_rerun_after_a_crash_before_prune_reuses_the_verified_archive() -> None:
    store = seed()
    sink = MemorySink()

    class CrashingStore(FakeStore):
        def delete_offsets(self, **kw: object) -> int:
            raise RuntimeError("connection lost")

    crashing = CrashingStore(list(store.rows.values()))
    first = handler(crashing, sink).handle(ModelDeadLetterPruneRequest())
    assert first.verdict == EnumDeadLetterPruneVerdict.FAILED
    written = dict(sink.objects)

    again = FakeStore(list(crashing.rows.values()))
    second = handler(again, sink).handle(ModelDeadLetterPruneRequest())
    assert second.verdict == EnumDeadLetterPruneVerdict.PRUNED
    assert sink.objects == written  # nothing archived twice
    assert sorted(again.deleted) == [10, 11, 12, 20]


def test_a_sink_that_leaves_the_host_refuses_a_plaintext_cipher_before_reading() -> (
    None
):
    store = seed()
    result = handler(
        store, MemorySink(requires_encryption=True), NoArchiveCipher()
    ).handle(ModelDeadLetterPruneRequest())
    assert result.verdict == EnumDeadLetterPruneVerdict.REFUSED
    assert store.read_days == []
    assert store.deleted == []


def test_a_retention_below_the_contract_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        ModelDeadLetterPruneRequest(retention_days=0)


def test_a_manifest_that_no_longer_parses_is_rewritten_not_raised() -> None:
    # A manifest left in the sink by an older schema (or a torn write) must not
    # crash the run: the window is archived again, verified, and only then pruned.
    store = seed()
    sink = MemorySink()
    stale = f"{DLQ}/partition=0/day={OLD.isoformat()}/offsets-10-11.manifest.json"
    sink.put(stale, b'{"schema_version": "an-older-format"}')
    result = handler(store, sink).handle(ModelDeadLetterPruneRequest())
    assert result.verdict == EnumDeadLetterPruneVerdict.PRUNED
    assert sorted(store.deleted) == [10, 11, 12, 20]
    rewritten = ModelDeadLetterWindowManifest.model_validate_json(sink.get(stale))
    assert rewritten.record_count == 2
