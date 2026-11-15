# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection ledger source reads work_ledger_rows only while its parity proves the kinds complete.

Every rule has a control that differs in one fact and answers the other way, so
removing the rule makes the control (or the rule's own test) fail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.handlers.handler_ledger_claims import owner_finished
from omnimarket.models.model_worktree_ledger_projection import (
    ModelLedgerProjectionSettings,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerParityStatus,
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerFreshness,
    ModelWorkLedgerParity,
    ModelWorkLedgerParityDay,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_lab_alarm.protocols import ProjectionUnreadableError
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_ledger_projection import (
    ProjectionLedgerReader,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
# Marks a parameter nobody passed, because None is itself an answer (no newest row).
UNSET = datetime.min.replace(tzinfo=UTC)
KINDS = ("CLAIM", "TERMINAL", "RELEASE")
CLAIM = "2026-11-14T09:00:00Z | CLAIM | lane=lane-a | ticket=OMN-5 | worktree=omni_worktrees/OMN-5/repo"
DONE = "2026-11-14T10:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-5 | outcome=done | worktree=removed:/w/OMN-5/repo"
KEPT = "2026-11-14T10:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-5 | outcome=partial | worktree=kept:/w/OMN-5/repo:x"
TREE = Path("/w/OMN-5/repo")
ROOT = Path("/w")


def _command(
    *, max_pages: int = 500, unmeasured_days_allowed: int = 1
) -> ModelWorktreeReconcileCommand:
    return ModelWorktreeReconcileCommand(
        host="lab-host",
        roots=("/w",),
        correlation_id=uuid4(),
        ledger_source="projection",
        ledger_projection=ModelLedgerProjectionSettings(
            bus_lane="dev",
            timeout_seconds=30.0,
            max_pages=max_pages,
            unmeasured_days_allowed=unmeasured_days_allowed,
        ),
    )


def parity_days(
    *, terminal: bool = True, unmeasured: int = 1, now: datetime = NOW
) -> tuple[ModelWorkLedgerParityDay, ...]:
    """The window's UTC days: the three days before `now` measured, `now`'s own day open."""
    measured = [
        ModelWorkLedgerParityDay(
            day=(now - timedelta(days=back)).date().isoformat(),
            exact=True,
            exact_kinds={"CLAIM": True, "RELEASE": True, "TERMINAL": terminal},
        )
        for back in (3, 2, 1)
    ]
    open_days = [
        ModelWorkLedgerParityDay(day=now.date().isoformat(), exact=None)
        for _ in range(unmeasured)
    ]
    return tuple(measured + open_days)


def _row(text: str) -> ModelWorkLedgerRowRecord:
    return ModelWorkLedgerRowRecord(
        row_id=f"id-{abs(hash(text))}",
        row_ts=datetime.fromisoformat(text.split(" | ")[0].replace("Z", "+00:00")),
        row_type=text.split(" | ")[1],
        row_lane="lane-a",
        text=text,
        raw_row=text,
        source="test",
    )


def terminal_answer(
    rows: tuple[str, ...],
    *,
    now: datetime = NOW,
    days: tuple[ModelWorkLedgerParityDay, ...] | None = None,
    newest: datetime | None = UNSET,
    matched: int | None = None,
    cursor: str | None = None,
    error: str | None = None,
    failed: bool = False,
) -> ModelScmTerminal:
    newest = now - timedelta(hours=1) if newest is UNSET else newest
    result = ModelWorkLedgerQueryResult(
        correlation_id=uuid4(),
        query=EnumWorkLedgerQueryKind.ROWS,
        window_since=now - timedelta(days=3),
        window_until=now,
        matched=len(rows) if matched is None else matched,
        rows=tuple(_row(text) for text in rows),
        freshness=ModelWorkLedgerFreshness(
            newest_row_ts=newest, newest_projected_at=newest, rows_read=len(rows)
        ),
        parity=ModelWorkLedgerParity(
            status=EnumWorkLedgerParityStatus.EXACT,
            days=parity_days(now=now) if days is None else days,
        ),
        next_cursor=cursor,
        error=error,
    )
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid4(),
            command_topic="onex.cmd.omnimarket.work-ledger-query-requested.v1",
            command_envelope_id=uuid4(),
            terminal_topic="onex.evt.omnimarket.work-ledger-query-completed.v1",
        ),
        failed=failed,
        payload=result.model_dump(mode="json"),
    )


class Pages:
    """A scripted query node: answers the requests in order and keeps what it was asked."""

    def __init__(self, *answers: ModelScmTerminal) -> None:
        self._answers = list(answers)
        self.requests: list[ModelWorkLedgerQueryRequest] = []

    def __call__(self, request: BaseModel, correlation_id: UUID) -> ModelScmTerminal:
        assert isinstance(request, ModelWorkLedgerQueryRequest)
        assert request.correlation_id == correlation_id
        self.requests.append(request)
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


def test_two_pages_are_followed_and_folded() -> None:
    pages = Pages(
        terminal_answer((CLAIM,), cursor="c1"), terminal_answer((DONE,), cursor=None)
    )
    read = ProjectionLedgerReader(call=pages).read(_command(), NOW)
    assert read is not None
    assert read.fresh is True
    assert read.finished_claims == (CLAIM,)
    assert owner_finished(TREE, ROOT, read) is True
    first, second = pages.requests
    assert first.cursor is None
    assert second.cursor == "c1"
    assert first.correlation_id != second.correlation_id
    assert first.kinds == KINDS
    assert first.since == NOW - timedelta(days=3)
    assert first.until == NOW


def test_a_kept_worktree_terminal_closes_the_claim_but_does_not_finish_it() -> None:
    read = ProjectionLedgerReader(call=Pages(terminal_answer((CLAIM, KEPT)))).read(
        _command(), NOW
    )
    assert read is not None
    assert read.fresh is True
    assert read.closed_claims == (CLAIM,)
    assert read.finished_claims == ()
    assert owner_finished(TREE, ROOT, read) is False


@pytest.mark.parametrize(
    ("terminal_exact", "fresh"),
    [(False, False), (True, True)],
    ids=["no", "yes-control"],
)
def test_a_kind_the_parity_does_not_hold_complete_leaves_the_ledger_unread(
    terminal_exact: bool, fresh: bool
) -> None:
    answer = terminal_answer((CLAIM, DONE), days=parity_days(terminal=terminal_exact))
    read = ProjectionLedgerReader(call=Pages(answer)).read(_command(), NOW)
    assert read is not None
    assert read.fresh is fresh
    if not fresh:
        assert read.reason == "projection_incomplete:TERMINAL@2026-11-12"
        assert read.finished_claims == ()
        assert owner_finished(TREE, ROOT, read) is False


@pytest.mark.parametrize(
    ("allowed", "fresh"), [(1, False), (2, True)], ids=["beyond", "within-control"]
)
def test_days_without_a_parity_receipt_beyond_the_allowance_leave_it_unread(
    allowed: int, fresh: bool
) -> None:
    answer = terminal_answer((CLAIM, DONE), days=parity_days(unmeasured=2))
    read = ProjectionLedgerReader(call=Pages(answer)).read(
        _command(unmeasured_days_allowed=allowed), NOW
    )
    assert read is not None
    assert read.fresh is fresh
    assert fresh or read.reason == "projection_unmeasured"


@pytest.mark.parametrize(
    ("newest", "reason"),
    [
        (None, "projection_empty"),
        (NOW - timedelta(hours=7), "projection_frozen"),
        (NOW + timedelta(hours=2), "projection_clock_skew"),
        (NOW - timedelta(hours=1), ""),
    ],
    ids=["no-newest", "frozen", "skew", "fresh-control"],
)
def test_freshness_of_the_window(newest: datetime | None, reason: str) -> None:
    read = ProjectionLedgerReader(
        call=Pages(terminal_answer((CLAIM,), newest=newest))
    ).read(_command(), NOW)
    assert read is not None
    assert read.reason == reason
    assert read.fresh is (reason == "")


def test_a_window_that_matched_no_row_is_unread_not_empty() -> None:
    read = ProjectionLedgerReader(call=Pages(terminal_answer((), matched=0))).read(
        _command(), NOW
    )
    assert read is not None
    assert read.reason == "projection_empty"


def test_an_error_answer_a_failed_terminal_and_a_foreign_payload_are_unread() -> None:
    errored = ProjectionLedgerReader(
        call=Pages(terminal_answer((CLAIM,), error="db down"))
    ).read(_command(), NOW)
    failed = ProjectionLedgerReader(
        call=Pages(terminal_answer((CLAIM,), failed=True))
    ).read(_command(), NOW)
    foreign = terminal_answer((CLAIM,)).model_copy(
        update={"payload": {"query": "rows"}}
    )
    garbled = ProjectionLedgerReader(call=Pages(foreign)).read(_command(), NOW)
    assert errored is not None
    assert errored.reason == "projection_error"
    assert failed is not None
    assert failed.reason == "projection_error"
    assert garbled is not None
    assert garbled.reason == "projection_unreadable"


def test_an_unreachable_bus_is_unread_and_never_raises() -> None:
    def down(request: BaseModel, correlation_id: UUID) -> ModelScmTerminal:
        raise ProjectionUnreadableError("no answer on lane dev")

    read = ProjectionLedgerReader(call=down).read(_command(), NOW)
    assert read is not None
    assert read.fresh is False
    assert read.reason == "projection_unreadable:ProjectionUnreadableError"


@pytest.mark.parametrize(
    ("max_pages", "fresh"), [(2, False), (3, True)], ids=["capped", "room-control"]
)
def test_a_window_past_the_page_cap_is_unread(max_pages: int, fresh: bool) -> None:
    pages = Pages(
        terminal_answer((CLAIM,), cursor="c1"),
        terminal_answer((), matched=1, cursor="c2"),
        terminal_answer((DONE,)),
    )
    read = ProjectionLedgerReader(call=pages).read(_command(max_pages=max_pages), NOW)
    assert read is not None
    assert read.fresh is fresh
    assert fresh or read.reason == "projection_too_large"


def test_an_open_claim_stays_live_with_no_heartbeat() -> None:
    old = "2026-11-13T00:00:00Z | CLAIM | lane=lane-a | ticket=OMN-5 | worktree=omni_worktrees/OMN-5/repo"
    read = ProjectionLedgerReader(call=Pages(terminal_answer((old,)))).read(
        _command(), NOW
    )
    assert read is not None
    assert read.live_claims == (old,)


def test_a_command_pairs_the_ledger_source_with_its_settings() -> None:
    settings = ModelLedgerProjectionSettings(bus_lane="dev", timeout_seconds=30.0)
    base = {"host": "h", "roots": ("/w",), "correlation_id": uuid4()}
    with pytest.raises(ValueError, match="requires ledger_projection"):
        ModelWorktreeReconcileCommand(**base, ledger_source="projection")
    with pytest.raises(ValueError, match="only used by ledger_source projection"):
        ModelWorktreeReconcileCommand(**base, ledger_projection=settings)
    # CONTROL: each source with its own settings is a valid command.
    ModelWorktreeReconcileCommand(
        **base, ledger_source="projection", ledger_projection=settings
    )
    ModelWorktreeReconcileCommand(**base, ledger_path="/ledger.md")


def test_the_example_overlay_loads_for_a_file_host_and_for_a_projection_host() -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_schedule import (
        load_overlay,
    )

    example = (
        Path(__file__).parents[1]
        / "src/omnimarket/nodes/node_worktree_reconcile_effect/models/overlay.example.yaml"
    )
    file_host = load_overlay(str(example), "lab-host").hosts["lab-host"]
    projection_host = load_overlay(str(example), "lab-host-projection").hosts[
        "lab-host-projection"
    ]
    assert file_host.ledger_source == "file"
    assert file_host.ledger_path == "/srv/onex/state/ledger.md"
    assert projection_host.ledger_source == "projection"
    assert projection_host.ledger_path is None
    assert projection_host.ledger_projection is not None
    assert projection_host.ledger_projection.required_kinds == KINDS
