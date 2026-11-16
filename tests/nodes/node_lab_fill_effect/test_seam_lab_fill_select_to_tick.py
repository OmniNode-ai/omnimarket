# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the select step's held tickets into the tick (OMN-19998).

Only JSON passes between the two, the way the bus carries it: the select result is dumped and validated again, its
``holds()`` ride on the tick request's ``selection_holds`` and the tick skips a held ticket with the selection's reason. A
field either side renames, or a hold the tick stops reading, fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_select import (
    HandlerLabFillSelect,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillTickRequest,
    ModelLabFillTickResult,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_select import (
    ModelLabFillSelectResult,
)

from .test_golden_chain_lab_fill_tick import _request, _row, _tick
from .test_lab_fill_select import HANDOFF_ROW, FakeLedgerRows, select_request

pytestmark = pytest.mark.unit


def _selected(rows: FakeLedgerRows) -> ModelLabFillSelectResult:
    answer = HandlerLabFillSelect(rows).handle(select_request())
    return ModelLabFillSelectResult.model_validate(answer.model_dump(mode="json"))


def _tick_request(tmp_path: Path, holds: dict[str, str]) -> ModelLabFillTickRequest:
    request = _request(tmp_path).model_copy(update={"selection_holds": holds})
    return ModelLabFillTickRequest.model_validate(request.model_dump(mode="json"))


def test_the_selections_holds_reach_the_tick(tmp_path: Path) -> None:
    holds = _selected(FakeLedgerRows(lines=(HANDOFF_ROW,))).holds()
    assert holds == {"OMN-20677": "handed-off"}
    handler, runner, _ = _tick([_row(20677), _row(9999)])
    result = handler.handle(_tick_request(tmp_path, holds))
    assert isinstance(result, ModelLabFillTickResult)
    assert [d.ticket for d in result.dispatched] == ["OMN-9999"]
    assert [(s.ticket, s.reason) for s in result.skipped] == [
        ("OMN-20677", "selection:handed-off")
    ]
    assert len(runner.calls) == 1


def test_without_the_holds_both_rows_are_dispatched(tmp_path: Path) -> None:
    """The failing control: the same rows with no hold dispatch the ticket the selection would have held."""
    handler, runner, _ = _tick([_row(20677), _row(9999)])
    result = handler.handle(_tick_request(tmp_path, {}))
    assert [d.ticket for d in result.dispatched] == ["OMN-20677", "OMN-9999"]
    assert len(runner.calls) == 2


def test_a_not_ok_ledger_read_holds_nothing_and_says_so() -> None:
    answer = _selected(FakeLedgerRows(status="unreadable", reason="bus down"))
    assert answer.holds() == {}
    assert answer.selection is None
    assert answer.ledger_status == "unreadable"


def test_holds_names_only_the_held_decisions() -> None:
    assert _selected(FakeLedgerRows(lines=())).holds() == {}
