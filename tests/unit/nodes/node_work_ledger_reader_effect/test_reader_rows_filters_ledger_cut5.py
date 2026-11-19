# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows read carries kinds and term to the query node, page by page (OMN-24555).

The reader's rows read may narrow by ledger row kind and by a substring term; the handler
sends both in every page's ModelPointQuery, and a request that names them on any other query
is refused. The seam test runs the real query node in process so a shape change on either
side fails here: the claim kind and the status term each answer only their own row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    answer_from_json,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderOverlay,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.runtime_work_ledger_reader import (
    main,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    WATERMARK,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_reader_effect/overlay.example.yaml"
)

TOPICS = {
    "reader_answered": "onex.evt.omnimarket.work-ledger-reader-answered.v1",
    "reader_refused": "onex.evt.omnimarket.work-ledger-reader-refused.v1",
}


def overlay(**fields: object) -> ModelReaderOverlay:
    base = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
    return base.model_copy(update=fields)


def rows_request(window_hours: int = 24, **fields: object) -> ModelReaderRequest:
    return ModelReaderRequest(
        reader="rows",
        query=EnumReaderQuery.ROWS,
        window_hours=window_hours,
        **fields,
    )


def reader(
    read: ProtocolLedgerPointRead, *, clock: datetime = NOW, **fields: object
) -> HandlerWorkLedgerReader:
    return HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",), **fields),
        read,
        InMemoryEventSink(),
        clock=lambda: clock,
        topics=TOPICS,
    )


class PagedPointRead:
    """Serves a list of pages in order, recording every request it is sent."""

    def __init__(self, pages: list[ModelPointAnswer]) -> None:
        self._pages = list(pages)
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        if not self._pages:
            return ModelPointAnswer(error="no-more-pages")
        page = self._pages.pop(0)
        if self._pages:
            page = page.model_copy(
                update={"next_cursor": f"cursor-{len(self.requests)}"}
            )
        return page


def test_filters_reach_every_page() -> None:
    page1 = ModelPointAnswer(
        lines=("line-1", "line-2"),
        matched=2,
        watermark=WATERMARK,
        next_cursor="cursor-1",
    )
    page2 = ModelPointAnswer(lines=("line-3",), matched=2, watermark=WATERMARK)
    read = PagedPointRead([page1, page2])
    result = reader(read).handle(rows_request(kinds=("CLAIM",), term="OMN-1"))
    assert result.status is EnumReaderStatus.OK, result.reason
    assert result.lines == ("line-1", "line-2", "line-3")
    assert len(read.requests) == 2
    for query in read.requests:
        assert query.kinds == ("CLAIM",)
        assert query.term == "OMN-1"


def test_rows_read_without_filters_sends_none() -> None:
    read = PagedPointRead(
        [ModelPointAnswer(lines=("line-1",), matched=1, watermark=WATERMARK)]
    )
    result = reader(read).handle(rows_request())
    assert result.status is EnumReaderStatus.OK, result.reason
    [query] = read.requests
    assert query.kinds == ()
    assert query.term is None


def test_filters_are_refused_off_a_rows_read() -> None:
    with pytest.raises(ValidationError, match="only for a rows read"):
        ModelReaderRequest(
            reader="inbox", query=EnumReaderQuery.INBOX, kinds=("CLAIM",)
        )
    with pytest.raises(ValidationError, match="only for a rows read"):
        ModelReaderRequest(
            reader="claims",
            query=EnumReaderQuery.OPEN_CLAIMS,
            window_hours=24,
            term="OMN-1",
        )


# The seam test's clock stands where test_reader_rows_window.py's does: window_hours = 27
# makes the reader's since the same SEAM_SINCE, inside the query node's bounded window.
SEAM_NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
SEAM_SINCE = datetime(2026, 11, 9, 0, 0, 0, tzinfo=UTC)
CLAIM_ROW = "2026-11-09T10:00:00Z | CLAIM | lane=lane-a | scenario=r1 | ticket=OMN-1"
STATUS_ROW = "2026-11-09T10:05:00Z | STATUS | lane=lane-a | scenario=r1 | green"


class QueryNodePointRead:
    """The real query node handler in process: the typed request in, its JSON answer through the
    adapter the real backend uses out, so a shape change on either side fails the seam test."""

    def __init__(self, node: HandlerWorkLedgerQuery) -> None:
        self._node = node
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        correlation = uuid4()
        payload = {
            "correlation_id": str(correlation),
            **query.model_dump(exclude_none=True),
        }
        for empty in ("lanes", "kinds"):
            if not payload.get(empty):
                payload.pop(empty, None)
        result = self._node.handle(ModelWorkLedgerQueryRequest.model_validate(payload))
        return answer_from_json(json.loads(result.model_dump_json()), str(correlation))


def test_query_node_seam_filters() -> None:
    store = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
    for number, raw in enumerate((CLAIM_ROW, STATUS_ROW)):
        assert store.ingest({"row_id": f"row-{number}", "raw_row": raw})
    read = QueryNodePointRead(
        HandlerWorkLedgerQuery(reader=store, now=lambda: SEAM_NOW)
    )

    claims = reader(read, clock=SEAM_NOW, page_size=2).handle(
        rows_request(window_hours=27, kinds=("CLAIM",))
    )
    assert claims.status is EnumReaderStatus.OK, claims.reason
    assert claims.lines == (CLAIM_ROW,)
    assert STATUS_ROW not in claims.lines

    # The term matches only the status row's own word, so the same read answers the reverse.
    status = reader(read, clock=SEAM_NOW, page_size=2).handle(
        rows_request(window_hours=27, term="green")
    )
    assert status.status is EnumReaderStatus.OK, status.reason
    assert status.lines == (STATUS_ROW,)
    assert CLAIM_ROW not in status.lines


def test_command_line_carries_kind_and_term_to_the_point_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The node's own entry point takes --kind (repeatable) and --term for a rows read."""
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
            "--kind",
            "CLAIM",
            "--kind",
            "TERMINAL",
            "--term",
            "OMN-1",
        ],
        environ={"ONEX_LEDGER_READER_OVERLAY": str(moved)},
        point_read=read,
    )
    assert code in (0, 1), capsys.readouterr().err
    [query] = read.requests
    assert query.kinds == ("CLAIM", "TERMINAL")
    assert query.term == "OMN-1"
