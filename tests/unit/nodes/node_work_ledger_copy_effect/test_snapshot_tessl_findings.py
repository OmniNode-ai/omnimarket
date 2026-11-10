# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tessl review findings on the snapshot job and restore drill (OMN-22916).

Each test failed on the branch before its fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_copy_effect.adapters import adapters_snapshot
from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_snapshot import (
    DirectorySnapshotStore,
    MemoryRowSource,
    MemorySnapshotStore,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumDrillOutcome,
    EnumSnapshotOutcome,
    ModelLedgerRow,
    ModelSnapshotBundle,
    ModelSnapshotManifest,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError
from tests.unit.nodes.node_work_ledger_copy_effect.support_snapshot import (
    NOW,
    RUN_ID,
    SOURCE_HOST,
    drill_handler,
    drill_request,
    rows,
    snapshot_handler,
    snapshot_request,
)

pytestmark = pytest.mark.unit


def _bundle(*, source_host: str, run_id: str = RUN_ID) -> ModelSnapshotBundle:
    return ModelSnapshotBundle(
        manifest=ModelSnapshotManifest.seal(run_id, NOW, source_host, rows(), rows()),
        topic_rows=rows(),
        projection_rows=rows(),
    )


def test_the_seal_covers_source_host_run_id_and_cutoff() -> None:
    manifest = _bundle(source_host=SOURCE_HOST).manifest
    assert manifest.sealed_ok()
    assert not manifest.model_copy(update={"source_host": "other.invalid"}).sealed_ok()
    assert not manifest.model_copy(update={"run_id": "20250101T000000Z"}).sealed_ok()
    assert not manifest.model_copy(
        update={"cutoff": NOW.replace(year=2025)}
    ).sealed_ok()


def test_the_drill_refuses_a_snapshot_taken_from_another_source_host() -> None:
    store = MemorySnapshotStore()
    store.bundles[RUN_ID] = _bundle(source_host="other.invalid")
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REFUSED_DAMAGED
    assert "source_host" in result.detail


def test_the_drill_refuses_a_snapshot_whose_manifest_names_another_run() -> None:
    store = MemorySnapshotStore()
    store.bundles[RUN_ID] = _bundle(source_host=SOURCE_HOST, run_id="20250101T000000Z")
    result = drill_handler(store).handle(drill_request())
    assert result.outcome is EnumDrillOutcome.REFUSED_DAMAGED
    assert "run_id" in result.detail


class _MovingTopic:
    """A topic source that gains a row between the first read and the next."""

    def __init__(self) -> None:
        self.reads = 0

    def rows(self) -> tuple[ModelLedgerRow, ...]:
        self.reads += 1
        return rows(5) if self.reads == 1 else rows(6)


def test_a_topic_that_moves_while_the_projection_is_read_is_not_snapshotted() -> None:
    store = MemorySnapshotStore()
    result = snapshot_handler(store, topic_source=_MovingTopic()).handle(
        snapshot_request()
    )
    assert result.outcome is EnumSnapshotOutcome.SOURCE_FAILED
    assert "changed" in result.detail
    assert store.bundles == {}


def test_a_non_ascii_digest_is_refused_where_the_manifest_is_built() -> None:
    with pytest.raises(ValidationError, match="digest"):
        ModelSnapshotManifest(
            run_id=RUN_ID,
            cutoff=NOW,
            source_host=SOURCE_HOST,
            topic_digests=("é",),
            projection_digests=(),
            manifest_sha256="0" * 64,
        )


def test_a_failed_write_leaves_no_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(adapters_snapshot.os, "replace", refuse)
    store = DirectorySnapshotStore(tmp_path)
    with pytest.raises(CopyStepError):
        store.write(_bundle(source_host=SOURCE_HOST))
    assert list(tmp_path.iterdir()) == []


class _PruneFails(MemorySnapshotStore):
    def prune(self, keep: int) -> tuple[str, ...]:
        raise CopyStepError("store", "could not prune 20250101T000000Z: busy")


def test_a_prune_failure_is_reported_on_the_written_result() -> None:
    result = snapshot_handler(_PruneFails()).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.WRITTEN
    assert "could not prune" in result.warning


def test_rows_that_differ_only_in_where_the_separator_sits_digest_differently() -> None:
    first = ModelLedgerRow(seq=1, row_id="a", text="b\x1fc")
    second = ModelLedgerRow(seq=1, row_id="a\x1fb", text="c")
    assert first.digest != second.digest


def test_the_snapshot_still_round_trips_for_a_valid_run() -> None:
    store = MemorySnapshotStore()
    assert snapshot_handler(store).handle(snapshot_request()).ok
    assert drill_handler(store).handle(drill_request()).outcome is (
        EnumDrillOutcome.PASSED
    )
    assert MemoryRowSource(rows()).rows() == rows()
