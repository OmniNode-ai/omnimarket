# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23669: the factory floor step reads its window from the read node, page by page; a short read is never a floor.

Golden chain: the declared reads page the recorded rows and give the floor the rows the file gave for the same period, and
the floor effect built from those pages draws the same floor model as the effect built from the file. Error chain (the
failing controls): a page that does not answer, a cursor still open after the declared pages, a spent time budget and an
unset read command each refuse the step naming the read as unread; a pool of one worker cannot run two reads together.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_floor_ledger_pages import (
    HandlerFloorLedgerPages,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_floor_ledger_pages import (
    ModelFloorLedgerPages,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    load_contract,
    parse_ledger_text,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)

from .test_line_board_facts import EVIDENCE, WINDOW_ROWS, _prepare, _Reader
from .test_runtime_factory_floor import CONTRACT, NOW, world
from .window_port import RecordedWindowPort

__all__ = ["world"]

pytestmark = pytest.mark.unit

HOUR = timedelta(hours=1)
THEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
KINDS = frozenset({"HOLD", "RELEASE", "TERMINAL", "CLAIM", "STATUS", "MSG"})
LINES = "\n".join(
    [
        "L1 2026-10-20T08:00:00Z | HOLD | lane=lane-a | id=H1 | ticket=OMN-1 | a hold",
        "L2 2026-10-31T11:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-1 | outcome=blocked | blocked on a person",
        "L3 2026-11-01T08:00:00Z | TERMINAL | lane=lane-b | ticket=OMN-2 | outcome=blocked | blocked again",
        "L4 2026-11-01T09:00:00Z | TERMINAL | lane=lane-c | ticket=OMN-3 | outcome=done | finished",
        "L5 2026-11-01T10:00:00Z | CLAIM | lane=lane-d | ticket=OMN-4 | a claim",
        "L6 2026-11-01T10:30:00Z | TERMINAL | lane=lane-e | ticket=OMN-5 | outcome=blocked | blocked a third time",
        "L7 2026-11-01T11:00:00Z | HOLD | lane=lane-a | id=H2 | ticket=OMN-1 | a second hold",
        "L8 2026-11-01T11:10:00Z | HOLD | lane=lane-a | id=H3 | ticket=OMN-1 | a third hold",
        "L9 2026-11-01T11:20:00Z | HOLD | lane=lane-a | id=H4 | ticket=OMN-1 | a fourth hold",
    ]
)


def _settings(**changes: Any) -> ModelFloorLedgerPages:
    document: dict[str, Any] = {
        "read_command_env": "ONEX_TEST_LEDGER_READ_COMMAND",
        "page_size": 2,
        "max_pages": 5,
        "page_timeout_seconds": 10,
        "time_budget_seconds": 60,
        "workers": 2,
        "reads": [
            {"name": "holds", "kinds": ["HOLD"], "window_hours": 336},
            {
                "name": "blocked",
                "kinds": ["TERMINAL"],
                "term": "outcome=blocked",
                "window_hours": 24,
            },
        ],
        **changes,
    }
    return ModelFloorLedgerPages.model_validate(document)


def _read(port: Any, settings: ModelFloorLedgerPages | None = None, **kw: Any) -> Any:
    return HandlerFloorLedgerPages().handle(
        settings or _settings(),
        window_days=14,
        kinds=KINDS,
        now=THEN,
        environ={},
        source=port,
        **kw,
    )


def test_golden_same_floor_rows_as_the_file_for_the_same_period() -> None:
    """The rows the pages give are the rows the file gave: those the declared reads ask for, from each read's own window."""
    port = RecordedWindowPort(LINES)
    result = _read(port)
    window_24h = THEN - 24 * HOUR
    wanted = [
        line
        for line in LINES.splitlines()
        if (" | HOLD | " in line)
        or (
            " | TERMINAL | " in line
            and "outcome=blocked" in line
            and line.split(" ", 1)[1][:20] >= window_24h.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    ]
    assert wanted, "the fixture holds rows for both reads"
    from_file = parse_ledger_text("\n".join(wanted), KINDS)
    assert [row.model_dump() for row in result.rows] == [
        row.model_dump() for row in from_file
    ]
    # The blocked terminal of 2026-10-31T11:00 is older than the read's 24 hours and is not read; the hold of 2026-10-20 is inside 14 days.
    assert {row.lane for row in result.rows if row.kind == "TERMINAL"} == {
        "lane-b",
        "lane-e",
    }


def test_golden_each_request_carries_the_declared_kinds_term_window_page_and_cursor() -> (
    None
):
    port = RecordedWindowPort(LINES)
    _read(port)
    holds = [q for q in port.requests if q.kinds == ("HOLD",)]
    blocked = [q for q in port.requests if q.kinds == ("TERMINAL",)]
    assert [q.cursor for q in holds] == [None, "2"]
    assert [q.cursor for q in blocked] == [None]
    assert {(q.page_size, q.query) for q in port.requests} == {(2, "rows")}
    assert all(q.until == THEN + timedelta(seconds=1) for q in port.requests)
    assert {q.since for q in holds} == {THEN - 336 * HOUR}
    assert {q.since for q in blocked} == {THEN - 24 * HOUR}
    assert {q.term for q in blocked} == {"outcome=blocked"}
    assert all(q.term is None for q in holds)


def test_golden_pages_are_followed_to_the_last_and_counted() -> None:
    result = _read(RecordedWindowPort(LINES))
    counts = {count.name: count for count in result.reads}
    assert (counts["holds"].pages, counts["holds"].lines) == (2, 4)
    assert (counts["blocked"].pages, counts["blocked"].lines) == (1, 2)
    assert result.pages == 3
    assert len(result.rows) == 6


class _MeetingPort:
    """A port that answers a read only once another read is asking at the same time."""

    def __init__(self, parties: int) -> None:
        self._barrier = threading.Barrier(parties, timeout=3)

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            return ModelPointAnswer(
                error="the other read never ran together with this one"
            )
        return ModelPointAnswer(lines=())


def test_golden_the_reads_run_together() -> None:
    result = _read(_MeetingPort(2), _settings(workers=2))
    assert result.pages == 2
    assert result.rows == ()


def test_failing_control_a_pool_of_one_worker_runs_the_reads_one_after_the_other() -> (
    None
):
    """The break: one worker serialises the reads, the first waits for a partner that cannot start, and the step is refused."""
    with pytest.raises(SourceRefusedError, match="never ran together"):
        _read(_MeetingPort(2), _settings(workers=1))


class _FailingPort(RecordedWindowPort):
    def __init__(self, text: str, error: str) -> None:
        super().__init__(text)
        self._error = error

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        if "TERMINAL" in query.kinds:
            return ModelPointAnswer(error=self._error)
        return super().read(query)


def test_error_a_page_that_does_not_answer_makes_its_read_unread_not_empty() -> None:
    with pytest.raises(
        SourceRefusedError,
        match=r"read blocked is unread: page 1 did not answer \(answer_too_large\)",
    ):
        _read(_FailingPort(LINES, "answer_too_large"))


def test_error_a_cursor_still_open_after_the_declared_pages_is_unread() -> None:
    with pytest.raises(
        SourceRefusedError,
        match=r"read holds is unread: 1 pages and the cursor is still open",
    ):
        _read(RecordedWindowPort(LINES), _settings(max_pages=1, workers=1))


def test_error_a_spent_time_budget_is_unread() -> None:
    clock = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    with pytest.raises(SourceRefusedError, match=r"60 s time budget ended before page"):
        _read(
            RecordedWindowPort(LINES),
            _settings(workers=1),
            monotonic=lambda: next(clock),
        )


def test_error_an_unset_read_command_with_no_port_is_refused_by_name() -> None:
    with pytest.raises(
        SourceRefusedError, match="ONEX_TEST_LEDGER_READ_COMMAND is not set"
    ):
        _read(None)


def test_error_a_read_that_fails_returns_no_rows() -> None:
    """Never a short result: the step raises, so no caller can take the pages that did arrive for the window."""
    outcome: list[Any] = []
    with pytest.raises(SourceRefusedError):
        outcome.append(_read(_FailingPort(LINES, "read-command-exit-1")))
    assert outcome == []


def test_error_two_reads_with_one_name_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate read names: holds"):
        _settings(
            reads=[
                {"name": "holds", "kinds": ["HOLD"]},
                {"name": "holds", "kinds": ["RELEASE"]},
            ]
        )


def test_golden_the_contract_declares_every_read_the_effect_pages() -> None:
    pages = load_contract(CONTRACT).runtime.ledger_pages
    assert pages.read_command_env == "ONEX_LEDGER_BUS_READ_COMMAND"
    assert [read.name for read in pages.reads] == [
        "holds",
        "releases",
        "rulings",
        "corrections",
        "status_gate",
        "status_integration",
        "status_task_map",
        "floor_owner_msgs",
        "orchestrator_asks",
        "ask_answers",
        "claims",
        "terminals",
        "lane_status",
    ]
    # Every kind the compute keeps is read by some declared read, and the time budget sits inside the step's 30 s.
    kept = set(load_contract(CONTRACT).runtime.ledger_kinds)
    asked = {kind for read in pages.reads for kind in read.kinds}
    assert asked <= kept
    assert pages.time_budget_seconds < 30
    assert pages.page_timeout_seconds <= pages.time_budget_seconds


def test_golden_the_effect_draws_the_same_floor_from_pages_as_from_the_file(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: the recorded window as pages and as the file give the same record, line for line."""
    files = _prepare(world, monkeypatch)
    # Every recorded row sits within the reads' windows of the run's clock.
    contract_pages = load_contract(CONTRACT).runtime.ledger_pages
    common = {
        "now": NOW,
        "home": str(world["home"]),
        "kb": str(world["kb"]),
        "unresolved_rows_file": str(files["archive"]),
    }
    from_file = HandlerFactoryFloorEffect(merged_running=_Reader(EVIDENCE)).handle(
        ModelFactoryFloorRunRequest(**common, ledger_rows_file=str(files["window"]))
    )
    file_record = json.loads(Path(from_file.output_path).read_text())
    port = RecordedWindowPort(WINDOW_ROWS)
    from_pages = HandlerFactoryFloorEffect(
        merged_running=_Reader(EVIDENCE), ledger_source=port
    ).handle(ModelFactoryFloorRunRequest(**common))
    pages_record = json.loads(Path(from_pages.output_path).read_text())
    assert len(port.requests) >= len(contract_pages.reads)
    for record in (file_record, pages_record):
        for health in record["sources"]["health"]:
            health.pop("read_at", None)
        record.pop("generated_at", None)
    # The two rows no declared read asks for (a FRICTION row and a MSG to nobody the floor reads) are the only difference, and
    # they are counted, not drawn: the lines, the floor and the limits are the same record.
    counted = {"ledger_rows", "ledger_newest_row"}
    assert {k: v for k, v in pages_record["sources"].items() if k in counted} == {
        "ledger_rows": 5,
        "ledger_newest_row": "2026-10-31T11:45:00Z",
    }
    assert {k: v for k, v in file_record["sources"].items() if k in counted} == {
        "ledger_rows": 7,
        "ledger_newest_row": "2026-10-31T11:46:00Z",
    }
    for record in (file_record, pages_record):
        for key in counted:
            record["sources"].pop(key)
    assert pages_record == file_record


def test_error_the_effect_refuses_the_floor_when_a_window_read_is_unread(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _prepare(world, monkeypatch)
    port = RecordedWindowPort(WINDOW_ROWS, fail_on="CLAIM")
    with pytest.raises(SourceRefusedError, match="read claims is unread"):
        HandlerFactoryFloorEffect(
            merged_running=_Reader(EVIDENCE), ledger_source=port
        ).handle(
            ModelFactoryFloorRunRequest(
                now=NOW,
                home=str(world["home"]),
                kb=str(world["kb"]),
                unresolved_rows_file=str(files["archive"]),
            )
        )
