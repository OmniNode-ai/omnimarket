# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A read asks for the page size its query names in the overlay, page by page (OMN-24572).

The overlay's ``page_size_by_query`` names, per query, the page size that read asks for in
place of ``page_size``: an open-claims page carries each claim's activity and is far larger
than a rows line, so its pages must be smaller to fit under the query node's answer bound.
Absent, every read keeps ``page_size``, so overlays written before the key still load; a
value outside 1 to 5000 or an unknown query name is refused at load.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
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


def claims_request(**fields: object) -> ModelReaderRequest:
    return ModelReaderRequest(
        reader="claims", query=EnumReaderQuery.OPEN_CLAIMS, **fields
    )


def holds_request(**fields: object) -> ModelReaderRequest:
    return ModelReaderRequest(reader="holds", query=EnumReaderQuery.HOLDS, **fields)


def reader(
    read: ProtocolLedgerPointRead, *, clock: datetime = NOW, **fields: object
) -> HandlerWorkLedgerReader:
    return HandlerWorkLedgerReader(
        overlay(moved_readers=("claims", "holds"), **fields),
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


def test_open_claims_pages_carry_their_page_size() -> None:
    page1 = ModelPointAnswer(
        lines=("claim-1", "claim-2"),
        matched=2,
        watermark=WATERMARK,
        next_cursor="cursor-1",
    )
    page2 = ModelPointAnswer(lines=("claim-3",), matched=2, watermark=WATERMARK)
    fields: dict[str, object] = {
        "page_size": 50,
        "page_size_by_query": {EnumReaderQuery.OPEN_CLAIMS: 7},
    }
    read = PagedPointRead([page1, page2])
    result = reader(read, **fields).handle(claims_request())
    assert result.status is EnumReaderStatus.OK, result.reason
    assert result.lines == ("claim-1", "claim-2", "claim-3")
    assert len(read.requests) == 2
    for query in read.requests:
        assert query.page_size == 7

    # A holds read has no page size of its own: it keeps the overlay's page_size.
    holds = PagedPointRead(
        [ModelPointAnswer(lines=("hold-1",), matched=1, watermark=WATERMARK)]
    )
    holds_result = reader(holds, **fields).handle(holds_request())
    assert holds_result.status is EnumReaderStatus.OK, holds_result.reason
    [query] = holds.requests
    assert query.page_size == 50


def test_default_page_size_by_query_sends_page_size() -> None:
    assert overlay().page_size_by_query == {}
    fields: dict[str, object] = {"page_size": 50}
    for request in (claims_request(), holds_request()):
        read = PagedPointRead(
            [ModelPointAnswer(lines=("line-1",), matched=1, watermark=WATERMARK)]
        )
        result = reader(read, **fields).handle(request)
        assert result.status is EnumReaderStatus.OK, result.reason
        [query] = read.requests
        assert query.page_size == 50


def test_page_size_by_query_out_of_bounds_or_unknown_query_is_refused() -> None:
    def raw(**fields: object) -> dict[str, object]:
        base = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
        out = dict(base.model_dump(mode="json"))
        out.update(fields)
        return out

    with pytest.raises(ValidationError, match="between 1 and 5000"):
        ModelReaderOverlay.model_validate(raw(page_size_by_query={"open-claims": 0}))
    with pytest.raises(ValidationError, match="between 1 and 5000"):
        ModelReaderOverlay.model_validate(raw(page_size_by_query={"open-claims": 5001}))
    with pytest.raises(ValidationError):
        ModelReaderOverlay.model_validate(raw(page_size_by_query={"nope": 5}))


def test_overlay_file_loads_page_size_by_query(tmp_path: Path) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        EXAMPLE.read_text(encoding="utf-8")
        + "page_size_by_query: {open-claims: 400}\n",
        encoding="utf-8",
    )
    loaded = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(path)})
    assert loaded.page_size_by_query == {EnumReaderQuery.OPEN_CLAIMS: 400}
