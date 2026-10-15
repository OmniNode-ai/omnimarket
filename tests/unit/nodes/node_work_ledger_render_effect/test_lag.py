# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lag tests: a late event after its repair, the lost last row, a source that
cannot supply a row (OMN-20275, M6 deliverable 2; tla/ledger_projection)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_work_ledger_render_effect.models.model_work_ledger_render import (
    EnumWorkLedgerBlockReason,
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


def _event(seq: int, offset: int, topic: str = "t") -> ModelWorkLedgerRenderRequest:
    return ModelWorkLedgerRenderRequest(
        kind=EnumWorkLedgerRenderKind.ROW, row=_ROWS[seq], topic=topic, offset=offset
    )


def _probe() -> ModelWorkLedgerRenderRequest:
    return ModelWorkLedgerRenderRequest(kind=EnumWorkLedgerRenderKind.PROBE)


def test_a_late_event_after_its_repair_is_dropped_as_a_duplicate(
    tmp_path: Path,
) -> None:
    node = make_node(make_file(tmp_path), FakeSource(_ROWS))
    node.handle(_event(1, 0))
    node.handle(_event(3, 1))  # gap at 2 repaired from the source
    late = node.handle(_event(2, 2))  # the event for 2 finally arrives
    assert late.dropped_duplicate is True
    assert late.committable is True
    assert file_seq_rows(tmp_path / "ledger.md") == [
        _ROWS[n].raw_row for n in (1, 2, 3)
    ]


def test_a_redelivered_event_is_a_duplicate_and_committable(tmp_path: Path) -> None:
    node = make_node(make_file(tmp_path), FakeSource(_ROWS))
    node.handle(_event(1, 0))
    again = node.handle(_event(1, 1))
    assert again.dropped_duplicate is True
    assert again.commit_offsets == {"t": 1}
    assert file_seq_rows(tmp_path / "ledger.md") == [_ROWS[1].raw_row]


def test_the_lost_last_row_is_found_by_the_head_probe(tmp_path: Path) -> None:
    node = make_node(make_file(tmp_path), FakeSource(_ROWS))
    for n in (1, 2, 3):
        node.handle(_event(n, n - 1))
    # seq 4 was appended but its event was never published: no later event shows it
    result = node.handle(_probe())
    assert result.repaired_seqs == (4,)
    assert result.watermark == 4


def test_the_probe_does_nothing_when_the_file_holds_the_head(tmp_path: Path) -> None:
    node = make_node(make_file(tmp_path), FakeSource(_ROWS))
    for n in (1, 2, 3, 4):
        node.handle(_event(n, n - 1))
    result = node.handle(_probe())
    assert result.repaired_seqs == ()
    assert result.rendered_seqs == ()
    assert result.source_head == 4


def test_a_row_the_source_cannot_supply_blocks_visibly_and_never_skips(
    tmp_path: Path,
) -> None:
    source = FakeSource({1: _ROWS[1], 3: _ROWS[3]})  # seq 2 is missing everywhere
    node = make_node(make_file(tmp_path), source)
    node.handle(_event(1, 0))
    result = node.handle(_event(3, 1))
    assert result.blocked is not None
    assert result.blocked.seq == 2
    assert result.blocked.reason is EnumWorkLedgerBlockReason.SOURCE_MISSING
    assert result.watermark == 1
    assert file_seq_rows(tmp_path / "ledger.md") == [_ROWS[1].raw_row]


def test_a_blocked_gap_retries_and_a_later_repair_lifts_the_block(
    tmp_path: Path,
) -> None:
    source = FakeSource({1: _ROWS[1], 3: _ROWS[3]})
    node = make_node(make_file(tmp_path), source)
    node.handle(_event(1, 0))
    node.handle(_event(3, 1))
    reads_before = len(source.row_reads)
    still = node.handle(_probe())
    assert still.blocked is not None
    assert len(source.row_reads) > reads_before  # it retried, not a silent wait
    source.rows[2] = _ROWS[2]
    lifted = node.handle(_probe())
    assert lifted.blocked is None
    assert lifted.watermark == 3
    assert file_seq_rows(tmp_path / "ledger.md") == [
        _ROWS[n].raw_row for n in (1, 2, 3)
    ]


def test_a_late_event_lifts_a_block(tmp_path: Path) -> None:
    source = FakeSource({1: _ROWS[1], 3: _ROWS[3]})
    node = make_node(make_file(tmp_path), source)
    node.handle(_event(1, 0))
    node.handle(_event(3, 1))
    lifted = node.handle(_event(2, 2))
    assert lifted.blocked is None
    assert lifted.watermark == 3


def test_a_seq_the_file_holds_with_another_row_id_blocks(tmp_path: Path) -> None:
    node = make_node(make_file(tmp_path), FakeSource(_ROWS))
    node.handle(_event(1, 0))
    clash = row(1, "2026-10-14T10:01:00Z | STATUS | lane=other | a different row")
    result = node.handle(
        ModelWorkLedgerRenderRequest(
            kind=EnumWorkLedgerRenderKind.ROW, row=clash, topic="t", offset=1
        )
    )
    assert result.blocked is not None
    assert result.blocked.reason is EnumWorkLedgerBlockReason.ROW_ID_CONFLICT
    assert result.blocked.seq == 1
    assert result.committable is False
    assert file_seq_rows(tmp_path / "ledger.md") == [_ROWS[1].raw_row]


def test_two_rows_with_equal_text_are_two_records(tmp_path: Path) -> None:
    same = "2026-10-14T10:00:00Z | STATUS | lane=x | same text"
    rows = {1: row(1, same), 2: row(2, same)}
    node = make_node(make_file(tmp_path), FakeSource(rows))
    for n in (1, 2):
        node.handle(
            ModelWorkLedgerRenderRequest(
                kind=EnumWorkLedgerRenderKind.ROW, row=rows[n], topic="t", offset=n
            )
        )
    assert file_seq_rows(tmp_path / "ledger.md") == [same, same]


def test_a_multi_line_row_blocks_rather_than_corrupt_the_watermark(
    tmp_path: Path,
) -> None:
    two_lines = "2026-10-14T10:00:00Z | STATUS | lane=x | first\ncontinuation"
    node = make_node(make_file(tmp_path), FakeSource({1: row(1, two_lines)}))
    result = node.handle(_probe())
    assert result.blocked is not None
    assert result.blocked.reason is EnumWorkLedgerBlockReason.MULTI_LINE_ROW
    assert file_seq_rows(tmp_path / "ledger.md") == []


def test_a_baseline_is_never_re_rendered(tmp_path: Path) -> None:
    rows = {n: row(n) for n in range(1, 8)}
    node = make_node(make_file(tmp_path, baseline=5), FakeSource(rows))
    before = node.handle(
        ModelWorkLedgerRenderRequest(
            kind=EnumWorkLedgerRenderKind.ROW, row=rows[3], topic="t", offset=0
        )
    )
    assert before.dropped_duplicate is True
    result = node.handle(_probe())
    assert result.repaired_seqs == (6, 7)
    assert file_seq_rows(tmp_path / "ledger.md") == [rows[6].raw_row, rows[7].raw_row]
