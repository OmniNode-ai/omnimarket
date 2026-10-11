# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_consumer_flow_prune_effect: archive, verify, then prune (OMN-19658).

The invariant under test is the one the operator's consent names: no row of
omninode_internal.consumer_flow_windows is deleted unless the archive object
holding it was read back from the sink, decrypted, checksummed and recounted,
and the rows the source still holds under those projection cursors encode to
exactly the archived bytes. Rows on the cutoff day and later are never read.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    ComplianceSweepRequest,
    NodeComplianceSweep,
)
from omnimarket.nodes.node_consumer_flow_prune_effect.handlers.handler_consumer_flow_prune import (
    HandlerConsumerFlowPrune,
    contract_config,
)
from omnimarket.nodes.node_consumer_flow_prune_effect.models import (
    EnumConsumerFlowDayStatus,
    EnumConsumerFlowPruneVerdict,
    ModelConsumerFlowDay,
    ModelConsumerFlowPruneRequest,
    ModelConsumerFlowRow,
    ModelConsumerFlowWindowManifest,
)
from omnimarket.topic_archive.live import NoArchiveCipher
from tests.sweep_corpus_fixture import init_fixture_repo
from tests.topic_archive_fakes import MemorySink, XorCipher

pytestmark = pytest.mark.unit

TABLE = "omninode_internal.consumer_flow_windows"
AS_OF = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
OLD = dt.date(2026, 9, 14)  # 36 days before AS_OF
EDGE = dt.date(2026, 9, 19)  # 31 days before AS_OF: whole day older than 30 days
NEW = dt.date(2026, 9, 20)  # the cutoff day itself is kept
NODE = "6f1c1d2e-0000-4000-8000-000000000001"


def row(
    cursor: int, day: dt.date, *, group: str = "g.consume.1", state: str = "IDLE"
) -> ModelConsumerFlowRow:
    start = dt.datetime.combine(day, dt.time(10, 0), tzinfo=dt.UTC) + dt.timedelta(
        seconds=30 * cursor
    )
    return ModelConsumerFlowRow(
        consumer_group=group,
        topic="onex.evt.omniclaude.tool-executed.v1",
        window_start=start,
        window_end=start + dt.timedelta(seconds=30),
        node_id=NODE,
        ingest_sequence=cursor,
        messages_in=0,
        messages_out=0,
        messages_dlq=0,
        handler_errors=None,
        upstream_produced=None,
        upstream_evidence="none",
        flow_state=state,
        evaluated_at=start + dt.timedelta(seconds=31),
        projection_cursor=cursor,
    )


class FakeStore:
    """consumer_flow_windows in memory, keyed on projection_cursor."""

    def __init__(self, rows: list[ModelConsumerFlowRow]) -> None:
        self.rows = {r.projection_cursor: r for r in rows}
        self.read_days: list[dt.date] = []
        self.deleted: list[int] = []
        self.drop_on_delete: int | None = None  # simulate a concurrent delete

    @staticmethod
    def _day(r: ModelConsumerFlowRow) -> dt.date:
        return r.window_start.astimezone(dt.UTC).date()

    def list_days(self, *, cutoff: dt.date) -> list[ModelConsumerFlowDay]:
        counts: dict[dt.date, int] = {}
        for r in self.rows.values():
            if self._day(r) < cutoff:
                counts[self._day(r)] = counts.get(self._day(r), 0) + 1
        return [
            ModelConsumerFlowDay(day=d, row_count=n) for d, n in sorted(counts.items())
        ]

    def day_cursors(self, *, day: dt.date) -> list[int]:
        self.read_days.append(day)
        return sorted(c for c, r in self.rows.items() if self._day(r) == day)

    def read_rows(
        self, *, day: dt.date, cursors: list[int]
    ) -> list[ModelConsumerFlowRow]:
        self.read_days.append(day)
        return [
            self.rows[c]
            for c in sorted(cursors)
            if c in self.rows and self._day(self.rows[c]) == day
        ]

    def delete_cursors(self, *, day: dt.date, cursors: list[int]) -> int:
        n = 0
        for c in cursors:
            if self.drop_on_delete == c:
                continue
            r = self.rows.get(c)
            if r is not None and self._day(r) == day:
                del self.rows[c]
                n += 1
                self.deleted.append(c)
        return n


class CorruptingSink(MemorySink):
    def get(self, name: str) -> bytes:
        data = super().get(name)
        if name.endswith(".manifest.json"):
            return data
        return data[:-1] + bytes([data[-1] ^ 0xFF])


def handler(
    store: FakeStore, sink: MemorySink | None = None, cipher: object | None = None
) -> HandlerConsumerFlowPrune:
    return HandlerConsumerFlowPrune(
        store=store,
        sink=sink if sink is not None else MemorySink(),
        cipher=cipher if cipher is not None else XorCipher(),  # type: ignore[arg-type]
        now=AS_OF,
        max_rows_per_object=2,
        delete_batch_size=2,
    )


def seed() -> FakeStore:
    rows = [row(c, OLD) for c in (10, 11, 12)]
    rows += [row(20, EDGE)]
    rows += [row(30, NEW), row(31, NEW)]
    return FakeStore(rows)


# --- contract ---------------------------------------------------------------


def test_contract_declares_thirty_day_retention_on_consumer_flow_windows() -> None:
    cfg = contract_config()
    assert cfg.retention_days == 30
    assert cfg.table == TABLE
    assert cfg.binding.archive_dir is None
    assert cfg.binding.database_url is None
    raw = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_consumer_flow_prune_effect/contract.yaml"
        ).read_text()
    )
    assert raw["node_type"] == "EFFECT_GENERIC"
    s3 = raw["config"]["consumer_flow_prune"]["sinks"]["s3"]
    assert s3["requires_encryption"] is True
    assert s3["prefix"] == "consumer-flow-archive/"


# --- the happy path -----------------------------------------------------------


def test_archives_then_prunes_only_whole_days_older_than_retention_cutoff() -> None:
    store = seed()
    result = handler(store).handle(ModelConsumerFlowPruneRequest())

    assert result.verdict == EnumConsumerFlowPruneVerdict.PRUNED
    assert result.cutoff_day == NEW
    assert sorted(store.deleted) == [10, 11, 12, 20]
    # rows on the cutoff day are never read and never deleted
    assert set(store.rows) == {30, 31}
    assert NEW not in store.read_days
    assert result.rows_pruned == 4
    assert all(d.status == EnumConsumerFlowDayStatus.PRUNED for d in result.days)


def test_a_store_that_lists_the_cutoff_day_is_refused_for_that_day() -> None:
    # Defence in depth: a store bug that lists a kept day must not reach a read.
    store = seed()

    class LeakyStore(FakeStore):
        def list_days(self, *, cutoff: dt.date) -> list[ModelConsumerFlowDay]:
            return [
                *super().list_days(cutoff=cutoff),
                ModelConsumerFlowDay(day=NEW, row_count=2),
            ]

    leaky = LeakyStore(list(store.rows.values()))
    result = handler(leaky).handle(ModelConsumerFlowPruneRequest())
    assert result.verdict == EnumConsumerFlowPruneVerdict.FAILED
    assert NEW not in leaky.read_days
    assert {30, 31} <= set(leaky.rows)


def test_each_object_round_trips_exact_with_a_manifest() -> None:
    store = seed()
    sink = MemorySink()
    original = {c: store.rows[c] for c in (10, 11, 12, 20)}
    handler(store, sink).handle(ModelConsumerFlowPruneRequest())

    manifests = [n for n in sink.objects if n.endswith(".manifest.json")]
    # 3 rows on OLD at 2 rows per object -> 2 objects; 1 on EDGE -> 1 object
    assert len(manifests) == 3
    back: dict[int, ModelConsumerFlowRow] = {}
    for m_name in manifests:
        m = ModelConsumerFlowWindowManifest.model_validate_json(sink.objects[m_name])
        assert m.source_table == TABLE
        assert m_name.startswith(f"{TABLE}/day=")
        plain = gzip.decompress(XorCipher().decrypt(sink.objects[m.object_name]))
        lines = [json.loads(line) for line in plain.splitlines()]
        assert len(lines) == m.record_count
        for obj in lines:
            r = ModelConsumerFlowRow.model_validate(obj)
            back[r.projection_cursor] = r
    assert back == original


# --- never prune an unverified row -------------------------------------------


def test_dry_run_reads_counts_only_and_writes_and_deletes_nothing() -> None:
    store = seed()
    sink = MemorySink()
    result = handler(store, sink).handle(ModelConsumerFlowPruneRequest(dry_run=True))
    assert result.verdict == EnumConsumerFlowPruneVerdict.DRY_RUN
    assert sink.objects == {}
    assert store.deleted == []
    assert store.read_days == []
    assert result.rows_eligible == 4
    assert all(d.status == EnumConsumerFlowDayStatus.DRY_RUN for d in result.days)


def test_nothing_older_than_the_cutoff_is_nothing_to_prune() -> None:
    store = FakeStore([row(30, NEW), row(31, NEW)])
    sink = MemorySink()
    result = handler(store, sink).handle(ModelConsumerFlowPruneRequest())
    assert result.verdict == EnumConsumerFlowPruneVerdict.NOTHING_TO_PRUNE
    assert sink.objects == {}
    assert set(store.rows) == {30, 31}


def test_a_corrupted_readback_fails_the_day_and_deletes_nothing() -> None:
    store = seed()
    result = handler(store, CorruptingSink()).handle(ModelConsumerFlowPruneRequest())
    assert result.verdict == EnumConsumerFlowPruneVerdict.FAILED
    assert store.deleted == []
    assert all(d.status == EnumConsumerFlowDayStatus.FAILED for d in result.days)


def test_a_row_rewritten_between_archive_and_prune_refuses_the_delete() -> None:
    # The projection upserts: a late heartbeat can rewrite an archived window.
    store = seed()

    class RewritingStore(FakeStore):
        calls = 0

        def read_rows(
            self, *, day: dt.date, cursors: list[int]
        ) -> list[ModelConsumerFlowRow]:
            rows = super().read_rows(day=day, cursors=cursors)
            self.calls += 1
            if self.calls > 1 and day == OLD:  # the pre-delete re-read
                rows = [
                    r.model_copy(update={"flow_state": "STALLED"}) if i == 0 else r
                    for i, r in enumerate(rows)
                ]
            return rows

    rewriting = RewritingStore(list(store.rows.values()))
    result = handler(rewriting).handle(ModelConsumerFlowPruneRequest())
    assert result.verdict == EnumConsumerFlowPruneVerdict.FAILED
    old = next(d for d in result.days if d.day == OLD)
    assert old.status == EnumConsumerFlowDayStatus.FAILED
    assert old.rows_pruned == 0
    assert {10, 11, 12} <= set(rewriting.rows)


def test_a_short_delete_is_reported_failed() -> None:
    store = seed()
    store.drop_on_delete = 11
    result = handler(store).handle(ModelConsumerFlowPruneRequest())
    assert result.verdict == EnumConsumerFlowPruneVerdict.FAILED
    day = next(d for d in result.days if d.day == OLD)
    assert day.status == EnumConsumerFlowDayStatus.FAILED
    assert "deleted 1 of 2" in day.detail


def test_a_rerun_after_a_crash_before_prune_reuses_the_verified_archive() -> None:
    store = seed()
    sink = MemorySink()

    class CrashingStore(FakeStore):
        def delete_cursors(self, *, day: dt.date, cursors: list[int]) -> int:
            raise RuntimeError("permission denied for table consumer_flow_windows")

    crashing = CrashingStore(list(store.rows.values()))
    first = handler(crashing, sink).handle(ModelConsumerFlowPruneRequest())
    assert first.verdict == EnumConsumerFlowPruneVerdict.FAILED
    assert "permission denied" in first.days[0].detail
    written = dict(sink.objects)

    again = FakeStore(list(crashing.rows.values()))
    second = handler(again, sink).handle(ModelConsumerFlowPruneRequest())
    assert second.verdict == EnumConsumerFlowPruneVerdict.PRUNED
    assert sink.objects == written  # nothing archived twice
    assert sorted(again.deleted) == [10, 11, 12, 20]


def test_a_sink_that_leaves_the_host_refuses_a_plaintext_cipher_before_reading() -> (
    None
):
    store = seed()
    result = handler(
        store, MemorySink(requires_encryption=True), NoArchiveCipher()
    ).handle(ModelConsumerFlowPruneRequest())
    assert result.verdict == EnumConsumerFlowPruneVerdict.REFUSED
    assert store.read_days == []
    assert store.deleted == []


def tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=60000,
    )


def test_the_contract_schedules_daily_on_the_runtime_tick() -> None:
    raw = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_consumer_flow_prune_effect/contract.yaml"
        ).read_text()
    )
    assert raw["input_subscriptions"] == [
        {
            "topic": "onex.intent.platform.runtime-tick.v1",
            "operation": "consumer_flow.prune_scheduled_run",
        }
    ]
    assert contract_config().schedule.run_interval_seconds == 86400


def test_a_scheduled_tick_runs_once_per_interval_and_skips_in_between() -> None:
    store = seed()
    h = HandlerConsumerFlowPrune(
        store=store,
        sink=MemorySink(),
        cipher=XorCipher(),  # type: ignore[arg-type]
        max_rows_per_object=2,
        delete_batch_size=2,
    )
    first = h.handle(tick(AS_OF))
    assert first.verdict == EnumConsumerFlowPruneVerdict.PRUNED
    assert first.cutoff_day == NEW  # measured from the tick's own clock
    assert sorted(store.deleted) == [10, 11, 12, 20]

    store.rows[40] = row(40, EDGE)
    assert h.handle(tick(AS_OF + dt.timedelta(hours=23))) is None
    assert 40 in store.rows

    store.rows[50] = row(50, NEW + dt.timedelta(days=1))
    later = h.handle(tick(AS_OF + dt.timedelta(hours=24)))
    assert later is not None
    assert later.verdict == EnumConsumerFlowPruneVerdict.PRUNED
    # the cutoff moves with the tick: NEW is now a whole day older than 30 days
    assert later.cutoff_day == NEW + dt.timedelta(days=1)
    assert set(store.rows) == {50}


def test_a_retention_below_the_contract_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 30"):
        ModelConsumerFlowPruneRequest(retention_days=29)


def test_unconfigured_sink_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("overlay_version: 1.0.0\nenvironment: test\nscope: env\n")
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    monkeypatch.delenv("ONEX_CONSUMER_FLOW_ARCHIVE_DIR", raising=False)
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
    h = HandlerConsumerFlowPrune()
    result = h.handle(ModelConsumerFlowPruneRequest(as_of=AS_OF))
    assert result.verdict == EnumConsumerFlowPruneVerdict.REFUSED
    assert "archive_dir" in result.detail
    assert result.sink_location is None
    assert result.rows_pruned == 0


def test_the_scheduled_run_result_has_a_declared_destination() -> None:
    """OMN-20867: a result with no publish topic made every runtime tick undeliverable.

    The boundary dead-lettered the tick and the DLQ replay put it back on the tick
    topic. The tick route now returns nothing inside the interval, and the run it
    does make is published on the declared completed topic.
    """
    raw = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_consumer_flow_prune_effect/contract.yaml"
        ).read_text()
    )
    completed = "onex.evt.omnimarket.consumer-flow-prune-completed.v1"
    assert raw["terminal_event"] == completed
    assert raw["event_bus"]["publish_topics"] == [completed]
    assert [e["topic"] for e in raw["published_events"]] == [completed]


def test_skipped_tick_never_resolves_sink() -> None:
    class UnresolvableSink(MemorySink):
        def __init__(self) -> None:
            self.requires_encryption = False

        @property
        def location(self) -> str:
            raise RuntimeError("sink resolution attempted")

    h = HandlerConsumerFlowPrune(
        store=FakeStore([]), sink=UnresolvableSink(), cipher=NoArchiveCipher()
    )
    first = tick(AS_OF)
    with pytest.raises(RuntimeError, match="sink resolution attempted"):
        h.handle(first)
    later = AS_OF + dt.timedelta(seconds=60)
    assert h.handle(tick(later)) is None


def test_the_contract_declares_the_database_transport_its_store_imports(
    tmp_path: Path,
) -> None:
    node = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_consumer_flow_prune_effect"
    )
    copy = tmp_path / "src" / "nodes" / node.name
    (copy / "handlers").mkdir(parents=True)
    (copy / "contract.yaml").write_text((node / "contract.yaml").read_text())
    for source in (node / "handlers").glob("*.py"):
        (copy / "handlers" / source.name).write_text(source.read_text())
    init_fixture_repo(tmp_path)

    result = NodeComplianceSweep().handle(
        ComplianceSweepRequest(
            target_dirs=[str(tmp_path)], checks=["undeclared-transport"]
        )
    )

    assert result.handlers_scanned >= 1
    assert result.by_type.get("UNDECLARED_TRANSPORT", 0) == 0, result.violations


def test_overlay_binding_archives_and_prunes_without_value_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_consumer_flow_prune_effect.handlers import (
        postgres_consumer_flow_store as store_module,
    )

    store = seed()
    seen: list[str] = []

    def make_store(dsn: str) -> FakeStore:
        seen.append(dsn)
        return store

    # Store construction is the effect boundary; the archive remains a real
    # owner-only filesystem sink, exercising archive/verify/delete end to end.
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(store_module, "PostgresConsumerFlowStore", make_store)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "overlay_version": "1.0.0",
                "environment": "test",
                "scope": "env",
                "services": {
                    "prune": {
                        "consumer_flow.archive_dir": str(tmp_path / "archive"),
                        "consumer_flow.database_url": "postgresql://fixture/contract-binding",
                    }
                },
            }
        )
    )
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
    h = HandlerConsumerFlowPrune()
    assert seen == []  # no binding or effects during runtime construction
    result = h.handle(ModelConsumerFlowPruneRequest(as_of=AS_OF))
    assert seen == ["postgresql://fixture/contract-binding"]
    assert result.verdict == EnumConsumerFlowPruneVerdict.PRUNED
    assert result.rows_pruned == 4
    assert list((tmp_path / "archive").rglob("*.manifest.json"))
    h.close()


def test_unconfigured_database_refuses_without_legacy_env_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("overlay_version: 1.0.0\nenvironment: test\nscope: env\n")
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", "postgresql://ignored/legacy")
    monkeypatch.setenv("OMNIBASE_INFRA_DB_URL", "postgresql://ignored/legacy")
    result = HandlerConsumerFlowPrune(sink=MemorySink()).handle(tick(AS_OF))
    assert result.verdict == EnumConsumerFlowPruneVerdict.REFUSED
    assert "database_url" in result.detail
    assert result.rows_pruned == 0


def test_refused_configuration_is_only_resolved_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_prune_binding_effect.handlers import (
        handler_prune_binding as module,
    )

    seen: list[str] = []

    def resolve(declared: object, kind: str) -> object:
        seen.append(kind)
        return declared

    monkeypatch.setattr(module, "load_prune_binding", resolve)
    h = HandlerConsumerFlowPrune()
    assert h.handle(tick(AS_OF)).verdict == EnumConsumerFlowPruneVerdict.REFUSED
    assert h.handle(tick(AS_OF + dt.timedelta(minutes=1))) is None
    assert seen == ["consumer_flow"]
    assert (
        h.handle(tick(AS_OF + dt.timedelta(days=1))).verdict
        == EnumConsumerFlowPruneVerdict.REFUSED
    )
    assert seen == ["consumer_flow", "consumer_flow"]


async def test_unconfigured_sink_refuses_through_runtime_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnibase_infra.enums import EnumDispatchStatus
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )
    from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef

    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("overlay_version: 1.0.0\nenvironment: test\nscope: env\n")
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    callback = _make_dispatch_callback(
        HandlerConsumerFlowPrune(),
        event_model=ModelHandlerRef(
            name="ModelRuntimeTick",
            module="omnibase_infra.runtime.models.model_runtime_tick",
        ),
    )
    incoming = tick(AS_OF)
    envelope = {
        "payload": incoming.model_dump(mode="json"),
        "correlation_id": str(incoming.correlation_id),
    }
    result = await callback(envelope)
    assert result is not None
    # Refusal is a successful dispatch carrying the typed domain outcome;
    # dispatch failure handling (retry/DLQ) has nothing to route.
    assert result.status == EnumDispatchStatus.SUCCESS
    assert result.correlation_id == incoming.correlation_id
    assert len(result.output_events) == 1
    assert result.output_events[0].verdict == EnumConsumerFlowPruneVerdict.REFUSED


def test_cli_unconfigured_database_writes_refusal_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_consumer_flow_prune_effect.__main__ import main

    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("overlay_version: 1.0.0\nenvironment: test\nscope: env\n")
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    reports = tmp_path / "reports"
    assert main(["--dry-run", "--report-dir", str(reports)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["verdict"] == "refused"
    assert "database_url" in summary["detail"]
    assert summary["pruned"] == 0
    assert len(list(reports.glob("*.json"))) == 1
    assert not (reports / "archive").exists()
