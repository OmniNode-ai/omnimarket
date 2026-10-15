# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Crash tests: between the row write and the offset commit, and mid-row with a torn
tail (OMN-20275, M6 deliverable 2; tla/ledger_projection)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_work_ledger_render_effect.handlers.rendered_ledger_file import (
    RenderedLedgerFile,
    WorkLedgerRenderLockHeldError,
)
from omnimarket.nodes.node_work_ledger_render_effect.models.model_work_ledger_render import (
    EnumWorkLedgerRenderKind,
    ModelWorkLedgerRenderRequest,
)
from tests.unit.nodes.node_work_ledger_render_effect.support import (
    FakeSource,
    file_seq_rows,
    make_file,
    make_node,
    row,
)

pytestmark = pytest.mark.unit

_ROWS = {n: row(n) for n in range(1, 5)}


def _event(seq: int, offset: int) -> ModelWorkLedgerRenderRequest:
    return ModelWorkLedgerRenderRequest(
        kind=EnumWorkLedgerRenderKind.ROW, row=_ROWS[seq], topic="t", offset=offset
    )


def test_a_crash_between_the_write_and_the_commit_costs_a_redelivery_not_a_row(
    tmp_path: Path,
) -> None:
    path = make_file(tmp_path)
    first = make_node(path, FakeSource(_ROWS))
    first.handle(_event(1, 0))
    first.close()  # the process dies here: the row is written, the offset is not committed
    second = make_node(path, FakeSource(_ROWS))
    assert second.watermark == 1  # derived from the file, not from a second file
    redelivered = second.handle(_event(1, 0))
    assert redelivered.dropped_duplicate is True
    assert redelivered.commit_offsets == {"t": 0}
    assert file_seq_rows(path) == [_ROWS[1].raw_row]


def test_a_torn_last_line_is_truncated_before_the_watermark_is_read(
    tmp_path: Path,
) -> None:
    path = make_file(tmp_path)
    first = make_node(path, FakeSource(_ROWS))
    first.handle(_event(1, 0))
    first.close()
    with path.open("ab") as handle:
        handle.write(_ROWS[2].raw_row[:25].encode())  # the crash tore row 2 mid-line
    second = make_node(path, FakeSource(_ROWS))
    assert second.watermark == 1
    assert file_seq_rows(path) == [_ROWS[1].raw_row]
    second.handle(_event(2, 1))
    assert file_seq_rows(path) == [_ROWS[1].raw_row, _ROWS[2].raw_row]


def test_a_crash_with_pending_events_rebuilds_them_from_redelivery(
    tmp_path: Path,
) -> None:
    path = make_file(tmp_path)
    source = FakeSource({1: _ROWS[1]})
    first = make_node(path, source)
    first.handle(_event(3, 5))  # waits past a gap the source cannot fill
    assert first.pending_count == 1
    first.close()
    second = make_node(path, FakeSource(_ROWS))
    assert second.pending_count == 0  # the pending set is volatile
    result = second.handle(_event(3, 5))  # redelivered; the gap is repaired from source
    assert result.watermark == 3
    assert file_seq_rows(path) == [_ROWS[n].raw_row for n in (1, 2, 3)]


def test_recovery_does_not_double_render_after_two_crashes(tmp_path: Path) -> None:
    path = make_file(tmp_path)
    for _ in range(2):
        node = make_node(path, FakeSource(_ROWS))
        node.handle(_event(1, 0))
        node.close()
    assert file_seq_rows(path) == [_ROWS[1].raw_row]


def test_the_watermark_is_the_baseline_plus_the_complete_rows(tmp_path: Path) -> None:
    path = make_file(tmp_path, baseline=10)
    rendered = RenderedLedgerFile(path)
    rendered.recover()
    assert rendered.baseline_seq == 10
    assert rendered.watermark == 10
    rendered.close()


def test_a_second_node_on_the_same_file_is_refused(tmp_path: Path) -> None:
    path = make_file(tmp_path)
    first = make_node(path, FakeSource(_ROWS))
    with pytest.raises(WorkLedgerRenderLockHeldError):
        make_node(path, FakeSource(_ROWS))
    first.close()
    make_node(path, FakeSource(_ROWS)).close()  # the lock is released with the node


def test_a_file_without_a_baseline_line_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.md"
    path.write_text("2026-10-14T10:00:00Z | STATUS | lane=x | no baseline\n")
    with pytest.raises(ValueError, match="baseline"):
        make_node(path, FakeSource({}))
