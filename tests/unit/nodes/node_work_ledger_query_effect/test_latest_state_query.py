# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The latest-state query through the work ledger query handler (OMN-23694).

Golden chain: a request names a subject kind and row types, the handler reads one keyset page of the
newest row per subject and row type, and the answer carries the entries, the page size, the watermark and a
cursor that continues the page. Error chain: a reader that cannot read, a reader with no latest table, a cursor
of another query and a request naming lanes and tickets together each answer an error and never an empty page.
The failing control is an answer over the bound: it is refused whole with the bounded form named.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.work_ledger_fold import fold_row
from omnimarket.handlers.work_ledger_latest_fold import (
    apply_latest,
    latest_entries_for_row,
    page_latest,
)
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerRowRecord
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerLatestEntry,
    ModelWorkLedgerLatestPage,
    ModelWorkLedgerLatestQuery,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)

pytestmark = pytest.mark.unit

_NODE = Path(__file__).parents[4] / "src/omnimarket/nodes/node_work_ledger_query_effect"
_PROJECTION = (
    Path(__file__).parents[4] / "src/omnimarket/nodes/node_projection_work_ledger"
)
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
WATERMARK = datetime(2026, 11, 11, 6, 59, tzinfo=UTC)

ROWS = [
    "2026-11-10T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-1) | work",
    "2026-11-10T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress",
    "2026-11-10T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | outcome=done | done",
    "2026-11-10T11:00:00Z | CLAIM | lane=beta | ticket=OMN-2 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-2) | work",
    "2026-11-10T11:30:00Z | TERMINAL | lane=beta | ticket=OMN-2 | friction=none | outcome=blocked | stop",
    "2026-11-10T12:00:00Z | MSG | from=beta | to=orchestrator | id=2026-11-10T12:00:00Z-beta | ticket=OMN-2 | need a call",
]


def _tracked() -> frozenset[str]:
    """The tracked row types the projection contract declares, never a literal."""
    contract = yaml.safe_load((_PROJECTION / "contract.yaml").read_text())
    return frozenset(contract["work_ledger"]["latest_row_types"])


def _row(raw: str) -> ModelWorkLedgerRowRecord:
    return fold_row(ModelWorkLedgerFoldRequest(raw_row=raw)).row


class LatestReader:
    """A reader over the pure fold: the in-memory twin of the table and its page statement."""

    def __init__(self, rows: list[str]) -> None:
        self.state: dict[tuple[str, str, str], ModelWorkLedgerLatestEntry] = {}
        for raw in rows:
            apply_latest(self.state, latest_entries_for_row(_row(raw), _tracked()))
        self.queries: list[ModelWorkLedgerLatestQuery] = []

    def read_latest_page(
        self, *, query: ModelWorkLedgerLatestQuery
    ) -> ModelWorkLedgerLatestPage:
        self.queries.append(query)
        page = page_latest(self.state, query)
        return page.model_copy(update={"watermark": WATERMARK})


class FailingReader(LatestReader):
    def read_latest_page(
        self, *, query: ModelWorkLedgerLatestQuery
    ) -> ModelWorkLedgerLatestPage:
        raise WorkLedgerReadError("database unreachable")


class NoLatestTableReader:
    """A reader with the row methods only: no latest table behind it."""

    def read_rows(self, *, since: datetime | None, until: datetime) -> tuple[()]:
        return ()


def _handler(reader: object) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(now=lambda: NOW, reader=reader)  # type: ignore[arg-type]


def _ask(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query=EnumWorkLedgerQueryKind.LATEST_STATE, **fields
    )


def _contract_page_size() -> int:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    return contract["work_ledger_query"]["latest_default_page_size"]


def test_golden_a_lane_terminal_read_answers_the_newest_terminal_per_lane() -> None:
    result = _handler(LatestReader(ROWS)).handle(
        _ask(subject_kind="lane", kinds=("TERMINAL",))
    )
    assert result.status == "ok"
    assert result.error is None
    assert [(e.subject, e.row_type, e.outcome) for e in result.latest] == [
        ("alpha", "TERMINAL", "done"),
        ("beta", "TERMINAL", "blocked"),
    ]
    assert result.matched == 2
    assert result.watermark == WATERMARK
    assert result.next_cursor is None
    assert result.page_size == _contract_page_size()


def test_golden_a_ticket_read_answers_every_tracked_row_type_of_the_ticket() -> None:
    result = _handler(LatestReader(ROWS)).handle(_ask(tickets=("OMN-2",)))
    assert [(e.subject_kind.value, e.subject, e.row_type) for e in result.latest] == [
        ("ticket", "OMN-2", "CLAIM"),
        ("ticket", "OMN-2", "MSG"),
        ("ticket", "OMN-2", "TERMINAL"),
    ]
    msg = next(e for e in result.latest if e.row_type == "MSG")
    assert msg.to == "orchestrator"
    assert msg.lane == "beta"


def test_golden_the_reader_is_asked_exactly_what_the_request_names() -> None:
    reader = LatestReader(ROWS)
    since = datetime(2026, 11, 10, 11, 0, tzinfo=UTC)
    _handler(reader).handle(
        _ask(
            lanes=("alpha", "beta"),
            kinds=("CLAIM", "TERMINAL"),
            since=since,
            page_size=50,
        )
    )
    (query,) = reader.queries
    assert query.subject_kind is not None
    assert query.subject_kind.value == "lane"
    assert query.subjects == ("alpha", "beta")
    assert query.row_types == ("CLAIM", "TERMINAL")
    assert query.since == since
    assert query.limit == 50
    assert query.after is None


def test_golden_paging_follows_next_cursor_without_repeat_or_gap() -> None:
    handler = _handler(LatestReader(ROWS))
    full = handler.handle(_ask(page_size=100)).latest
    assert len(full) > 3
    seen: list[ModelWorkLedgerLatestEntry] = []
    cursor: str | None = None
    for _ in range(len(full)):
        page = handler.handle(_ask(page_size=2, cursor=cursor))
        assert 1 <= len(page.latest) <= 2
        seen.extend(page.latest)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert cursor is None
    assert seen == list(full)


def test_error_chain_a_reader_that_cannot_read_answers_an_error_never_an_empty_page() -> (
    None
):
    result = _handler(FailingReader([])).handle(_ask())
    assert result.status == "error"
    assert result.error is not None
    assert "database unreachable" in result.error
    assert result.latest == ()
    assert result.next_cursor is None


def test_error_chain_a_reader_with_no_latest_table_answers_an_error() -> None:
    result = _handler(NoLatestTableReader()).handle(_ask())
    assert result.status == "error"
    assert result.error is not None
    assert "work_ledger_latest" in result.error
    assert result.latest == ()


def test_error_chain_a_cursor_of_another_query_is_an_error() -> None:
    other = _handler(LatestReader(ROWS)).handle(_ask(page_size=1)).next_cursor
    assert other is not None
    forged = other[:-2] + "xx"
    result = _handler(LatestReader(ROWS)).handle(_ask(cursor=forged))
    assert result.status == "error"
    assert result.latest == ()


def test_error_chain_lanes_and_tickets_together_are_refused() -> None:
    result = _handler(LatestReader(ROWS)).handle(
        _ask(lanes=("alpha",), tickets=("OMN-1",))
    )
    assert result.status == "error"
    assert result.error is not None
    assert "not both" in result.error


def test_error_chain_latest_fields_on_another_query_are_refused_by_the_request() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=EnumWorkLedgerQueryKind.ROWS,
            tickets=("OMN-1",),
        )


def test_failing_control_an_answer_over_the_bound_is_refused_whole_with_the_bounded_form() -> (
    None
):
    handler = _handler(LatestReader(ROWS))
    handler._answer_bound = handler._answer_bound.model_copy(
        update={"max_answer_bytes": 400}
    )
    result: ModelWorkLedgerQueryResult = handler.handle(_ask(page_size=100))
    assert result.status == "refused"
    assert result.latest == ()
    assert result.refusal is not None
    assert "page_size" in result.refusal.suggestion
    assert "next_cursor" in result.refusal.suggestion


def test_the_default_page_fits_the_declared_answer_bound_at_a_full_page() -> None:
    """A full default page of worst-case-sized entries is under the bound the contract declares."""
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    bound = contract["work_ledger_query"]["answer_bound"]["max_answer_bytes"]
    entry = ModelWorkLedgerLatestEntry(
        subject_kind="ticket",
        subject="OMN-123456",
        row_type="TERMINAL",
        row_id="f" * 64,
        at=NOW,
        lane="a-long-lane-name-3850-with-suffix",
        ticket="OMN-123456",
        pr="omnimarket#123456",
        outcome="handed-off",
        to="orchestrator-31d9,landing-controller",
    )
    full = ModelWorkLedgerQueryResult(
        correlation_id=uuid4(),
        query=EnumWorkLedgerQueryKind.LATEST_STATE,
        window_since=None,
        window_until=NOW,
        latest=(entry,) * _contract_page_size(),
        page_size=_contract_page_size(),
    )
    assert len(full.model_dump_json().encode()) < bound
