# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the thermal hold report: the guard's files in, the ledger row out.

Seam 1 (guard host script -> this node): the literals in conftest.py are the guard's real file shapes. A guard that renames
a key the node reads turns the read into the typed shape_changed error, and this file fails when the node stops reading
the keys the guard writes.
Seam 2 (this node -> the ledger and the tick): the alarm row passes the ledger row grammar, and has the cells the lane
inbox reader takes (the tick prints an open MSG addressed to its lane as a MISSING inbox line).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_thermal_hold_report_effect.adapters import (
    GuardFileState,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.handlers import (
    row_with_req_last,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.models import (
    EnumGuardReadState,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.handlers.handler_ledger_row_grammar import (
    HandlerLedgerRowGrammar,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.models.model_ledger_row_grammar import (
    ModelLedgerRowGrammarRequest,
)

from .conftest import ABSENT, LAST, LATCH, OUTBOX_ROW, present

pytestmark = pytest.mark.unit


def _files(tmp_path: Path, overlay) -> GuardFileState:
    return GuardFileState(
        tmp_path,
        overlay.latch_file,
        overlay.last_file,
        overlay.outbox_file,
        overlay.refused_file,
        overlay.claim_suffix,
    )


def _refusals(row: str) -> list[str]:
    return (
        HandlerLedgerRowGrammar()
        .handle(ModelLedgerRowGrammarRequest(operation="row_refusals", line=row))
        .refusals
    )


def test_the_guards_latch_and_last_files_are_read_as_written(tmp_path, overlay) -> None:
    (tmp_path / overlay.latch_file).write_text(json.dumps(LATCH))
    (tmp_path / overlay.last_file).write_text(json.dumps(LAST))
    files = _files(tmp_path, overlay)
    assert files.read_latch().state is EnumGuardReadState.PRESENT
    assert files.read_latch().data == LATCH
    assert files.read_last().data == LAST


@pytest.mark.parametrize("renamed", ["tripped_at", "hottest_c"])
def test_a_guard_that_renames_a_latch_key_reads_as_shape_changed(
    tmp_path, overlay, renamed
) -> None:
    changed = {("x_" + k if k == renamed else k): v for k, v in LATCH.items()}
    (tmp_path / overlay.latch_file).write_text(json.dumps(changed))
    read = _files(tmp_path, overlay).read_latch()
    assert read.state is EnumGuardReadState.SHAPE_CHANGED
    assert renamed in read.detail


def test_a_last_file_without_its_keys_and_a_latch_that_is_not_json(
    tmp_path, overlay
) -> None:
    (tmp_path / overlay.last_file).write_text(json.dumps({"at": "x"}))
    (tmp_path / overlay.latch_file).write_text("{not json")
    files = _files(tmp_path, overlay)
    assert files.read_last().state is EnumGuardReadState.SHAPE_CHANGED
    assert files.read_latch().state is EnumGuardReadState.UNREADABLE
    assert (
        _files(tmp_path / "none", overlay).read_latch().state
        is EnumGuardReadState.ABSENT
    )


def test_the_guards_outbox_row_is_claimed_and_settled_back_byte_for_byte(
    tmp_path, overlay
) -> None:
    (tmp_path / overlay.outbox_file).write_text(OUTBOX_ROW + "\n")
    files = _files(tmp_path, overlay)
    assert files.claim_outbox() == [OUTBOX_ROW]
    assert not (tmp_path / overlay.outbox_file).exists()
    (tmp_path / overlay.outbox_file).write_text(
        "late\n"
    )  # the guard appends after the claim
    files.settle_outbox([OUTBOX_ROW], [])
    assert (tmp_path / overlay.outbox_file).read_text() == f"late\n{OUTBOX_ROW}\n"


def _alarm_rows(handler, guard, appender, request_for) -> list[str]:
    guard.latch = present(LATCH)
    handler.handle(request_for())
    guard.latch = ABSENT
    handler.handle(request_for())
    return [row for _, row in appender.sent]


def test_the_alarm_rows_pass_the_ledger_row_grammar(
    handler, guard, appender, request_for
) -> None:
    trip, release = _alarm_rows(handler, guard, appender, request_for)
    assert _refusals(trip) == []
    assert _refusals(release) == []
    broken = trip.replace(" | from=example-thermal-hold-lab-a", "")
    assert _refusals(broken)  # the check can fail


def test_the_guards_outbox_row_passes_the_grammar_after_its_req_cell_moves() -> None:
    row, _ = row_with_req_last(OUTBOX_ROW)
    assert _refusals(row) == []


def test_the_alarm_row_has_the_cells_the_lane_inbox_reader_takes(
    handler, guard, appender, request_for
) -> None:
    trip, _ = _alarm_rows(handler, guard, appender, request_for)
    cells = trip.split(" | ")
    assert cells[1] == "MSG"
    by_key = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in cells[2:-1] if "=" in c}
    assert by_key["to"] == "example-orchestrator"
    assert by_key["id"] == f"{cells[0]}-{by_key['from']}"
    assert cells[-2].startswith("req=")
    assert " | " not in cells[-1]
