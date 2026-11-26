# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A history range read pages to the end of its range under its own declared bound.

The host overlay bounds an ordinary open set at ``max_pages`` (150 pages of 1000 rows); a
history range of 14 days or more holds more rows than that. ``range_max_pages`` is the bound a
range read (``since`` named) pages under; the packaged overlay declares it, no code constant
does, and an overlay that omits it keeps the ``max_pages`` bound for range reads too.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

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
# The host overlay's page bound for an open set.
HOST = {"max_pages": 150, "page_size": 1000}


class EndlessPages:
    """Serves ``total`` pages of one row each, then ends the set."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        n = len(self.requests)
        return ModelPointAnswer(
            lines=(f"row-{n}",),
            matched=self.total,
            watermark=WATERMARK,
            next_cursor=f"cursor-{n}" if n < self.total else None,
        )


def reader(read: EndlessPages, **fields: object) -> HandlerWorkLedgerReader:
    base = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
    ov = base.model_copy(update={"moved_readers": ("r",), **HOST, **fields})
    return HandlerWorkLedgerReader(
        ov, read, InMemoryEventSink(), clock=lambda: NOW, topics=TOPICS
    )


def range_request() -> ModelReaderRequest:
    return ModelReaderRequest(
        reader="r",
        query=EnumReaderQuery.ROWS,
        since=NOW - timedelta(days=30),
    )


def test_packaged_overlay_declares_a_range_bound_above_the_host_page_bound() -> None:
    packaged = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
    assert packaged.range_max_pages is not None
    assert packaged.range_max_pages > HOST["max_pages"]


def test_a_range_read_pages_past_max_pages_to_the_end_of_its_range() -> None:
    read = EndlessPages(total=400)
    result = reader(read).handle(range_request())
    assert result.status is EnumReaderStatus.OK, result.reason
    assert len(result.lines) == 400
    assert len(read.requests) == 400


def test_a_range_longer_than_its_own_bound_is_refused_never_cut_short() -> None:
    read = EndlessPages(total=400)
    result = reader(read, range_max_pages=300).handle(range_request())
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "open-set-longer-than-300-pages" in result.reason
    assert result.lines == ()


def test_a_read_without_a_range_keeps_the_max_pages_bound() -> None:
    read = EndlessPages(total=400)
    request = ModelReaderRequest(
        reader="r", query=EnumReaderQuery.ROWS, window_hours=24
    )
    result = reader(read).handle(request)
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "open-set-longer-than-150-pages" in result.reason


def test_an_overlay_without_the_key_bounds_a_range_read_by_max_pages() -> None:
    read = EndlessPages(total=400)
    result = reader(read, range_max_pages=None).handle(range_request())
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "open-set-longer-than-150-pages" in result.reason


def test_the_range_bound_is_validated() -> None:
    base = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
    data = dict(base.model_dump(mode="json"))
    data["range_max_pages"] = 0
    with pytest.raises(ValueError, match="range_max_pages"):
        ModelReaderOverlay.model_validate(data)
