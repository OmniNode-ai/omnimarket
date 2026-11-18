# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lines-only rows page carries the lines and no typed rows (OMN-23977).

The query node's paged rows answer carries each row twice, as typed records and as text lines;
a lines-only page carries the lines and no rows, so a page under the answer bound holds more
rows. The reader's overlay key rows_lines_only asks the query node for lines-only pages on a
rows read; the key is optional and off when absent, so every overlay document written before
it still loads.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderStatus,
    ModelPointAnswer,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    EXAMPLE,
    MSG_ROW,
    OUTSIDE_ROW,
    SEAM_NOW,
    SEAM_SINCE,
    STATUS_ROWS,
    PagedPointRead,
    QueryNodePointRead,
    overlay,
    reader,
    rows_request,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind


def _node(rows: tuple[str, ...]) -> HandlerWorkLedgerQuery:
    read = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
    for number, raw in enumerate(rows):
        assert read.ingest({"row_id": f"row-{number}", "raw_row": raw})
    return HandlerWorkLedgerQuery(reader=read, now=lambda: SEAM_NOW)


def test_golden_a_lines_only_page_carries_lines_and_no_rows() -> None:
    node = _node((*STATUS_ROWS, MSG_ROW, OUTSIDE_ROW))
    lines_only = node.handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=K.ROWS,
            since=SEAM_SINCE,
            page_size=100,
            lines_only=True,
        )
    )
    full = node.handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=K.ROWS,
            since=SEAM_SINCE,
            page_size=100,
        )
    )
    assert lines_only.rows == ()
    assert len(lines_only.lines) == 6
    assert lines_only.lines == full.lines
    assert len(full.rows) == 6
    assert len(json.dumps(lines_only.model_dump(mode="json"))) < len(
        json.dumps(full.model_dump(mode="json"))
    )


def test_seam_the_reader_with_rows_lines_only_reads_the_same_window_through_the_query_node() -> (
    None
):
    node = _node((*STATUS_ROWS, MSG_ROW, OUTSIDE_ROW))
    read = QueryNodePointRead(node)
    result = reader(read, clock=SEAM_NOW, page_size=2, rows_lines_only=True).handle(
        rows_request(window_hours=27)
    )
    assert result.status is EnumReaderStatus.OK, result.reason

    read_false = QueryNodePointRead(node)
    result_false = reader(
        read_false, clock=SEAM_NOW, page_size=2, rows_lines_only=False
    ).handle(rows_request(window_hours=27))
    assert result.lines == result_false.lines
    assert all(q.lines_only for q in read.requests)
    assert all(not q.lines_only for q in read_false.requests)


def test_the_overlay_key_is_optional_and_off_when_absent(tmp_path: Path) -> None:
    assert overlay().rows_lines_only is False
    # The example overlay loads with the key off; dropping the key still loads with False.
    dropped = "\n".join(
        line
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if not line.startswith("rows_lines_only")
    )
    path = tmp_path / "overlay.yaml"
    path.write_text(dropped, encoding="utf-8")
    loaded = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(path)})
    assert loaded.rows_lines_only is False


def test_error_chain_lines_only_without_a_page_size_or_on_another_query_is_refused() -> (
    None
):
    with pytest.raises(ValidationError, match="lines_only"):
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(), query=K.ROWS, lines_only=True
        )
    with pytest.raises(ValidationError, match="lines_only"):
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=K.HOLDS,
            page_size=10,
            lines_only=True,
        )


def test_error_chain_a_lines_only_page_over_the_node_fault_is_never_an_empty_answer() -> (
    None
):
    read = PagedPointRead([ModelPointAnswer(error="node-error:boom")])
    result = reader(read, rows_lines_only=True).handle(rows_request(window_hours=27))
    assert result.status is EnumReaderStatus.UNREADABLE
    assert result.lines == ()
