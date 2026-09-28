# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_board_truth_reconcile_effect: ledger + facts in, diff table out.

The chain runs the whole readback: parse a ledger, merge its claims, project,
render. This is the surface that replaces the manual board sweep, so the chain
asserts the two properties that make a readback trustworthy — it is idempotent,
and it never writes.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16732: node_board_truth_reconcile_effect
    - OMN-16735: retires the manual sweep in favour of reading this table
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
    ModelLinearStateFact,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.handlers.handler_board_truth_reconcile import (
    HandlerBoardTruthReconcile,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_input import (
    ModelBoardReconcileInput,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_output import (
    ModelBoardReconcileOutput,
)

_INSTANT = datetime(2026, 8, 27, 11, 30, 0, tzinfo=UTC)
_CORRELATION = UUID("00000000-0000-4000-8000-000000016732")

# Row shapes copied from the live ledger, including the two that a stricter
# parser would drop: one whose tickets appear only in the prose body, and one
# that cites no ticket at all.
_LEDGER = """\
| 2026-08-26T09:00:00Z | build-lane-a | OMN-90001 | CLAIM | building |
2026-08-24T09:00:00Z | build-lane-b | OMN-90002 | CLAIM | building |
2026-08-24T18:00:00Z | build-lane-b | OMN-90002 | TERMINAL | abandoned, nothing landed |
| 2026-08-27T08:00:00Z | body-cited-lane | CLAIM | OMN-90003 picked up in the body only |
2026-08-27T08:30:00Z | no-ticket-lane | CLAIM | archaeology, cites nothing |
"""


def _state(name: str) -> ModelLinearStateFact:
    return ModelLinearStateFact(
        state_name=name,
        state_type="started",
        set_at=_INSTANT - timedelta(hours=3),
        set_by_automation=None,
    )


def _bundle(ticket: str, state_name: str) -> ModelBoardFactBundle:
    return ModelBoardFactBundle(
        ticket=ticket,
        current_state=_state(state_name),
        ledger_claims=(),
        pull_requests=(),
        branches=(),
        facts_complete=True,
    )


_TICKETS = (
    _bundle("OMN-90001", "In Review"),
    _bundle("OMN-90002", "In Progress"),
    _bundle("OMN-90003", "Backlog"),
)


def _reconcile(ledger_path: Path | None) -> ModelBoardReconcileOutput:
    return HandlerBoardTruthReconcile().handle(
        ModelBoardReconcileInput(
            correlation_id=_CORRELATION,
            evaluated_at=_INSTANT,
            staleness_days=3,
            ledger_path=ledger_path,
            tickets=_TICKETS,
        )
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ROLLING_WORK_LEDGER.md"
    path.write_text(_LEDGER, encoding="utf-8")
    return path


@pytest.mark.unit
class TestBoardTruthReconcileGoldenChain:
    """Ledger + board facts in, diff table out."""

    def test_chain_binds_claims_and_reaps_the_abandoned_one(self, ledger: Path) -> None:
        output = _reconcile(ledger)
        rows = {row.ticket: row for row in output.projection.rows}

        # A live claim holds a ticket in progress even with no PR.
        assert rows["OMN-90001"].derived_state.value == "in_progress"
        # A claim that reached TERMINAL with nothing to show gets reaped.
        assert rows["OMN-90002"].derived_state.value == "backlog"
        assert any("TERMINAL" in item for item in rows["OMN-90002"].evidence), (
            "a reaper row must cite the terminated claim"
        )
        # Tickets cited only in a row's prose body still bind.
        assert rows["OMN-90003"].derived_state.value == "in_progress"

    def test_chain_renders_a_table_a_person_can_act_on(self, ledger: Path) -> None:
        report = _reconcile(ledger).report
        assert "BOARD-TRUTH DRY RUN" in report
        assert "not armed" in report
        assert "OMN-90002" in report
        assert "DISCREPANCIES" in report

    def test_chain_is_idempotent(self, ledger: Path) -> None:
        """Reading the board twice must say the same thing both times."""
        assert _reconcile(ledger).report == _reconcile(ledger).report

    def test_chain_runs_without_a_ledger(self) -> None:
        """A missing ledger degrades to board facts only, never to an error."""
        output = _reconcile(None)
        assert output.summary.ledger_claims_parsed == 0
        assert output.summary.evaluated_count == len(_TICKETS)

    def test_chain_performs_no_writes(self, ledger: Path) -> None:
        """The output is a report and nothing else — there is no write path."""
        output = _reconcile(ledger)
        assert set(output.model_dump().keys()) == {"projection", "report", "summary"}
