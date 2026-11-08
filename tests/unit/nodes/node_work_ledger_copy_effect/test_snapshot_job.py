# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Snapshot job (OMN-22916): golden chain and error chain."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_snapshot import (
    CommandRowSource,
    DirectorySnapshotStore,
    MemorySnapshotStore,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import EnumSnapshotOutcome
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError
from tests.unit.nodes.node_work_ledger_copy_effect.support_snapshot import (
    RUN_ID,
    SOURCE_HOST,
    DroppingStore,
    RecordingRunner,
    rows,
    snapshot_handler,
    snapshot_request,
)

pytestmark = pytest.mark.unit


def test_golden_snapshot_writes_both_tables_seals_them_and_reads_back() -> None:
    store = MemorySnapshotStore()
    result = snapshot_handler(store).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.WRITTEN
    assert (result.topic_rows, result.projection_rows) == (5, 5)
    held = store.bundles[RUN_ID]
    assert held.manifest.manifest_sha256 == result.manifest_sha256
    assert held.manifest.source_host == SOURCE_HOST
    assert held.topic_rows == rows()


def test_snapshot_prunes_to_the_overlay_keep() -> None:
    store = MemorySnapshotStore()
    for hour in range(5):
        snapshot_handler(store).handle(snapshot_request(f"20261107T0{hour}0000Z"))
    assert sorted(store.bundles) == [
        "20261107T020000Z",
        "20261107T030000Z",
        "20261107T040000Z",
    ]


def test_snapshot_onto_the_source_host_is_refused_before_any_read() -> None:
    store = MemorySnapshotStore()
    result = snapshot_handler(store, store_host=SOURCE_HOST).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.REFUSED_SAME_HOST
    assert store.bundles == {}


def test_an_empty_source_is_refused_and_writes_nothing() -> None:
    store = MemorySnapshotStore()
    result = snapshot_handler(store, topic=()).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.REFUSED_EMPTY
    assert store.bundles == {}


def test_a_failing_export_is_source_failed() -> None:
    runner = RecordingRunner({"export": (2, "")})
    source = CommandRowSource(["export", "topic"], 5, run=runner)
    store = MemorySnapshotStore()
    result = snapshot_handler(store, topic_source=source).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.SOURCE_FAILED
    assert "export exited 2" in result.detail
    assert store.bundles == {}


def test_named_break_storage_drops_a_row_is_caught_by_the_read_back() -> None:
    result = snapshot_handler(DroppingStore()).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.READBACK_MISMATCH


def test_directory_store_refuses_to_overwrite_a_snapshot_and_reports_a_store_failure(
    tmp_path: Path,
) -> None:
    store = DirectorySnapshotStore(tmp_path)
    assert snapshot_handler(store).handle(snapshot_request()).ok
    again = snapshot_handler(store).handle(snapshot_request())
    assert again.outcome is EnumSnapshotOutcome.STORE_FAILED
    assert "already exists" in again.detail
    with pytest.raises(CopyStepError):
        store.read("../escape")
