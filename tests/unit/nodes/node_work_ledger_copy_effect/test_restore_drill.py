# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Restore drill (OMN-22916): the table rebuilt from the accepted-row topic equals the projection, row for row;
a snapshot with one damaged row is refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_snapshot import (
    DirectorySnapshotStore,
    MemorySnapshotStore,
    SqliteTableRebuilder,
)
from omnimarket.nodes.node_work_ledger_copy_effect.handlers import (
    HandlerWorkLedgerRestoreDrill,
    HandlerWorkLedgerSnapshot,
    find_damaged_rows,
)
from omnimarket.nodes.node_work_ledger_copy_effect.handlers import (
    handler_work_ledger_snapshot as handler_module,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumDrillOutcome,
    ModelLedgerRow,
    ModelSnapshotManifest,
)
from tests.unit.nodes.node_work_ledger_copy_effect.support_snapshot import (
    DRILL_HOST,
    NOW,
    RUN_ID,
    SOURCE_HOST,
    SOURCE_PORT,
    FakeDrillReporter,
    FakeProbe,
    MemoryRowSource,
    drill_handler,
    drill_request,
    rows,
    snapshot_request,
    written_store,
)

pytestmark = pytest.mark.unit


def test_drill_rebuilds_the_table_equal_row_for_row_and_reports_once() -> None:
    reporter = FakeDrillReporter()
    probe = FakeProbe()
    result = drill_handler(written_store(), probe=probe, reporter=reporter).handle(
        drill_request()
    )
    assert result.outcome is EnumDrillOutcome.PASSED
    assert result.ok
    assert result.rebuilt_rows == 5
    assert probe.asked == [(SOURCE_HOST, SOURCE_PORT)]
    assert reporter.reports == [result]
    assert f"drill_host={DRILL_HOST}" in result.receipt()
    assert "rebuilt_rows=5" in result.receipt()


def test_drill_with_no_run_id_takes_the_newest_snapshot() -> None:
    store = written_store()
    older = MemoryRowSource(rows(3))
    HandlerWorkLedgerSnapshot(
        source_host=SOURCE_HOST,
        store_host=DRILL_HOST,
        topic=older,
        projection=older,
        store=store,
        keep=3,
    ).handle(snapshot_request("20261107T130000Z"))
    result = drill_handler(store).handle(drill_request(None))
    assert result.run_id == RUN_ID
    assert result.rebuilt_rows == 5


def test_directory_store_survives_the_round_trip_through_files(tmp_path: Path) -> None:
    store = DirectorySnapshotStore(tmp_path / "snapshots")
    source = MemoryRowSource(rows(8))
    made = HandlerWorkLedgerSnapshot(
        source_host=SOURCE_HOST,
        store_host=DRILL_HOST,
        topic=source,
        projection=source,
        store=store,
        keep=3,
    ).handle(snapshot_request())
    assert made.ok
    result = HandlerWorkLedgerRestoreDrill(
        drill_host=DRILL_HOST,
        source_host=SOURCE_HOST,
        source_port=SOURCE_PORT,
        store=store,
        probe=FakeProbe(),
        rebuilder=SqliteTableRebuilder("drill_rows"),
        reporter=FakeDrillReporter(),
    ).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.PASSED
    assert result.rebuilt_rows == 8


def _damage_row(store: MemorySnapshotStore, *, table: str, index: int) -> None:
    bundle = store.bundles[RUN_ID]
    held = list(getattr(bundle, table))
    held[index] = ModelLedgerRow(
        seq=held[index].seq, row_id=held[index].row_id, text="altered"
    )
    store.bundles[RUN_ID] = bundle.model_copy(update={table: tuple(held)})


@pytest.mark.parametrize("table", ["topic_rows", "projection_rows"])
def test_damaged_row_is_refused_and_named(table: str) -> None:
    store = written_store()
    _damage_row(store, table=table, index=2)
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REFUSED_DAMAGED
    assert not result.ok
    assert result.rebuilt_rows == 0
    assert len(result.damaged) == 1
    assert "row 2 (seq 3)" in result.damaged[0]


def test_damaged_snapshot_missing_a_row_is_refused() -> None:
    store = written_store()
    bundle = store.bundles[RUN_ID]
    store.bundles[RUN_ID] = bundle.model_copy(
        update={"topic_rows": bundle.topic_rows[:-1]}
    )
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REFUSED_DAMAGED
    assert "topic: 4 rows held, 5 sealed" in result.damaged[0]


def test_damaged_manifest_that_no_longer_matches_its_own_seal_is_refused() -> None:
    store = written_store()
    bundle = store.bundles[RUN_ID]
    forged = ModelSnapshotManifest.model_validate(
        {**bundle.manifest.model_dump(), "manifest_sha256": "0" * 64}
    )
    store.bundles[RUN_ID] = bundle.model_copy(update={"manifest": forged})
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REFUSED_DAMAGED
    assert "manifest" in result.damaged[0]


def test_damaged_byte_on_disk_is_refused(tmp_path: Path) -> None:
    store = DirectorySnapshotStore(tmp_path)
    source = MemoryRowSource(rows(6))
    HandlerWorkLedgerSnapshot(
        source_host=SOURCE_HOST,
        store_host=DRILL_HOST,
        topic=source,
        projection=source,
        store=store,
        keep=3,
    ).handle(snapshot_request())
    topic_file = tmp_path / RUN_ID / "topic.jsonl"
    topic_file.write_text(
        topic_file.read_text().replace("row 4 | text", "row 4 | texT")
    )
    result = HandlerWorkLedgerRestoreDrill(
        drill_host=DRILL_HOST,
        source_host=SOURCE_HOST,
        source_port=SOURCE_PORT,
        store=store,
        probe=FakeProbe(),
        rebuilder=SqliteTableRebuilder("drill_rows"),
        reporter=FakeDrillReporter(),
    ).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REFUSED_DAMAGED
    assert "row 3 (seq 4)" in result.damaged[0]


def test_damaged_control_with_the_check_removed_the_same_snapshot_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named injected break ``verification_removed``: the refusal reading above goes red."""
    store = written_store()
    _damage_row(store, table="topic_rows", index=2)
    assert find_damaged_rows(store.bundles[RUN_ID])
    monkeypatch.setattr(handler_module, "find_damaged_rows", lambda _bundle: ())
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is not EnumDrillOutcome.REFUSED_DAMAGED


def test_a_source_that_answers_from_the_drill_host_refuses_the_drill() -> None:
    result = drill_handler(written_store(), probe=FakeProbe(reachable=True)).handle(
        drill_request()
    )
    assert result.outcome is EnumDrillOutcome.REFUSED_SOURCE_REACHABLE


def test_a_probe_that_cannot_tell_refuses_the_drill_rather_than_assuming_unreachable() -> (
    None
):
    result = drill_handler(written_store(), probe=FakeProbe(unknowable=True)).handle(
        drill_request()
    )
    assert result.outcome is EnumDrillOutcome.REFUSED_PROBE_FAILED


def test_the_drill_refuses_to_run_on_the_source_host() -> None:
    result = drill_handler(written_store(), drill_host=SOURCE_HOST).handle(
        drill_request()
    )
    assert result.outcome is EnumDrillOutcome.REFUSED_SAME_HOST


def test_no_snapshot_on_storage_is_refused() -> None:
    result = drill_handler(MemorySnapshotStore()).handle(drill_request(None))
    assert result.outcome is EnumDrillOutcome.REFUSED_NO_SNAPSHOT
    result = drill_handler(MemorySnapshotStore()).handle(
        drill_request("20250101T000000Z")
    )
    assert result.outcome is EnumDrillOutcome.REFUSED_NO_SNAPSHOT


def test_named_break_row_in_the_projection_alone_fails_the_comparison() -> None:
    """Named injected break ``projection_only_row``: today's defect, a row that never reached the topic."""
    store = MemorySnapshotStore()
    topic = rows(5)
    projection = (
        *rows(5),
        ModelLedgerRow(seq=6, row_id="id-6", text="bypassed the judge"),
    )
    HandlerWorkLedgerSnapshot(
        source_host=SOURCE_HOST,
        store_host=DRILL_HOST,
        topic=MemoryRowSource(topic),
        projection=MemoryRowSource(projection),
        store=store,
        keep=3,
    ).handle(snapshot_request())
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REBUILD_MISMATCH
    assert "rebuilt 5 rows, projection holds 6" in result.detail
    assert "seq [6]" in result.detail


def test_named_break_row_withheld_from_the_topic_fails_the_comparison() -> None:
    store = MemorySnapshotStore()
    HandlerWorkLedgerSnapshot(
        source_host=SOURCE_HOST,
        store_host=DRILL_HOST,
        topic=MemoryRowSource(rows(5)[:-1]),
        projection=MemoryRowSource(rows(5)),
        store=store,
        keep=3,
    ).handle(snapshot_request())
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REBUILD_MISMATCH


def test_the_manifest_cutoff_is_the_request_time() -> None:
    assert written_store().bundles[RUN_ID].manifest.cutoff == NOW
