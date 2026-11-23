# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows read names lanes, and the query node keeps only those lanes' rows (OMN-26433).

The duplicate guard reads every row of the last hour (they name the live lanes) and then the claim rows of those lanes over the
window: a page or two where the whole window needs dozens. The seam test runs the real query node in process, so a shape change
on either side fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.runtime_work_ledger_reader import (
    main,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_filters_ledger_cut5 import (
    CLAIM_ROW,
    EXAMPLE,
    SEAM_NOW,
    STATUS_ROW,
    WATERMARK,
    PagedPointRead,
    QueryNodePointRead,
    reader,
    rows_request,
)

pytestmark = pytest.mark.unit

OTHER_LANE_CLAIM = (
    "2026-11-09T10:10:00Z | CLAIM | lane=lane-b | scenario=r1 | ticket=OMN-2"
)


def test_lanes_reach_every_page() -> None:
    read = PagedPointRead(
        [
            ModelPointAnswer(
                lines=("a",), matched=2, watermark=WATERMARK, next_cursor="c1"
            ),
            ModelPointAnswer(lines=("b",), matched=2, watermark=WATERMARK),
        ]
    )
    result = reader(read).handle(rows_request(lanes=("lane-a", "lane-b")))
    assert result.status is EnumReaderStatus.OK, result.reason
    assert len(read.requests) == 2
    assert all(q.lanes == ("lane-a", "lane-b") for q in read.requests)


def test_query_node_seam_keeps_only_the_named_lanes_rows() -> None:
    store = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
    for number, raw in enumerate((CLAIM_ROW, STATUS_ROW, OTHER_LANE_CLAIM)):
        assert store.ingest({"row_id": f"row-{number}", "raw_row": raw})
    read = QueryNodePointRead(
        HandlerWorkLedgerQuery(reader=store, now=lambda: SEAM_NOW)
    )

    answer = reader(read, clock=SEAM_NOW, page_size=5).handle(
        rows_request(window_hours=27, kinds=("CLAIM",), lanes=("lane-b",))
    )

    assert answer.status is EnumReaderStatus.OK, answer.reason
    assert answer.lines == (OTHER_LANE_CLAIM,)


def test_lanes_are_refused_off_a_rows_read() -> None:
    with pytest.raises(ValidationError, match="only for a rows read"):
        ModelReaderRequest(
            reader="claims",
            query=EnumReaderQuery.OPEN_CLAIMS,
            window_hours=24,
            lanes=("lane-a",),
        )


def test_command_line_carries_row_lanes_to_the_point_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = tmp_path / "overlay.yaml"
    moved.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "moved_readers: [inbox]", "moved_readers: [rows]"
        ),
        encoding="utf-8",
    )
    read = PagedPointRead(
        [ModelPointAnswer(lines=("claim-line",), matched=1, watermark=WATERMARK)]
    )
    code = main(
        [
            "--reader",
            "rows",
            "--query",
            "rows",
            "--window-hours",
            "24",
            "--row-lane",
            "lane-a",
            "--row-lane",
            "lane-b",
        ],
        environ={"ONEX_LEDGER_READER_OVERLAY": str(moved)},
        point_read=read,
    )
    assert code in (0, 1), capsys.readouterr().err
    [query] = read.requests
    assert query.lanes == ("lane-a", "lane-b")
