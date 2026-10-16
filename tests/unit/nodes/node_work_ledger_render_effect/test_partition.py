# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Partition tests: one ledger split across topics and delivered out of order,
and a broker outage (OMN-20275, M6 deliverable 2; tla/ledger_projection)."""

from __future__ import annotations

from pathlib import Path

import pytest

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

_ROWS = {n: row(n) for n in range(1, 6)}
_A = "topic-a"
_B = "topic-b"


def _event(seq: int, topic: str, offset: int) -> ModelWorkLedgerRenderRequest:
    return ModelWorkLedgerRenderRequest(
        kind=EnumWorkLedgerRenderKind.ROW, row=_ROWS[seq], topic=topic, offset=offset
    )


def _probe() -> ModelWorkLedgerRenderRequest:
    return ModelWorkLedgerRenderRequest(kind=EnumWorkLedgerRenderKind.PROBE)


def test_rows_split_across_topics_render_in_seq_order(tmp_path: Path) -> None:
    node = make_node(make_file(tmp_path), FakeSource(_ROWS))
    # seq 2 sits alone on topic B and arrives first; seqs 1,3 are on topic A
    for req in (_event(2, _B, 0), _event(3, _A, 1), _event(1, _A, 0)):
        result = node.handle(req)
    assert file_seq_rows(tmp_path / "ledger.md") == [
        _ROWS[1].raw_row,
        _ROWS[2].raw_row,
        _ROWS[3].raw_row,
    ]
    assert result.watermark == 3


def test_an_event_past_the_gap_waits_and_is_not_committable(tmp_path: Path) -> None:
    source = FakeSource({})  # the ledger of record cannot supply seq 1 yet
    node = make_node(make_file(tmp_path), source)
    result = node.handle(_event(2, _A, 0))
    assert result.watermark == 0
    assert result.committable is False
    assert result.commit_offsets == {}
    assert result.blocked is not None
    assert result.blocked.seq == 1


def test_offsets_commit_only_over_the_rendered_prefix_per_topic(
    tmp_path: Path,
) -> None:
    source = FakeSource({})
    node = make_node(make_file(tmp_path), source)
    node.handle(_event(3, _A, 0))
    node.handle(_event(2, _B, 0))
    result = node.handle(_event(1, _A, 1))
    # seq 1 arrives at offset 1 on A behind seq 3 at offset 0: the prefix on A is
    # [seq 3 (pending until 2 renders)] -> after 1,2,3 all rendered, both commit
    assert result.watermark == 3
    assert result.commit_offsets == {_A: 1, _B: 0}


def test_a_blocked_prefix_commits_nothing_past_the_gap(tmp_path: Path) -> None:
    node = make_node(make_file(tmp_path), FakeSource({}))
    node.handle(_event(1, _A, 0))
    node.handle(_event(3, _A, 1))  # gap at 2, source cannot supply it
    result = node.handle(_event(4, _A, 2))
    assert result.watermark == 1
    assert result.commit_offsets == {}  # seq 1 was committed earlier, 3 and 4 wait


def test_a_broker_outage_leaves_a_gap_the_probe_repairs(tmp_path: Path) -> None:
    source = FakeSource(_ROWS)
    node = make_node(make_file(tmp_path), source)
    node.handle(_event(1, _A, 0))
    # the broker is out: seqs 2..5 are never delivered; the head is read from the source
    result = node.handle(_probe())
    assert result.source_head == 5
    assert result.watermark == 5
    assert result.repaired_seqs == (2, 3, 4, 5)
    assert file_seq_rows(tmp_path / "ledger.md") == [
        _ROWS[n].raw_row for n in range(1, 6)
    ]


def test_an_unreachable_source_during_an_outage_blocks_and_then_lifts(
    tmp_path: Path,
) -> None:
    source = FakeSource({1: _ROWS[1], 3: _ROWS[3]})
    node = make_node(make_file(tmp_path), source)
    node.handle(_event(1, _A, 0))
    node.handle(_event(3, _A, 1))  # seq 2 is not supplied yet: blocked
    source.rows.update({2: _ROWS[2], 4: _ROWS[4], 5: _ROWS[5]})
    source.down = True
    blocked = node.handle(_probe())
    assert blocked.head_unavailable is True
    assert blocked.blocked is not None
    assert blocked.blocked.seq == 2
    assert blocked.watermark == 1
    source.down = False
    lifted = node.handle(_probe())
    assert lifted.blocked is None
    assert lifted.watermark == 5
    assert lifted.repaired_seqs == (2, 4, 5)
