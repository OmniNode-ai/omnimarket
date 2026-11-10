# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End to end: a lane's final message through the close node and the orchestrator's row writer to the ledger (OMN-23417).

Fix R1 of the diagnosed-red-stays-red plan: the remote lane's closing row carries the typed ``needs=`` cell and the lane's
``report=`` text, so the stopped-lane listing and the error-rule consumer can act on what the lane found. Every row the
orchestrator hands to the ledger writer is also judged by the ledger's own row grammar (the writer's admission rule).

Failing control (``drop-forwarding``): a close node whose result keeps its outcome, problem and delegation cells and
forwards no other cell is the unfixed runner's shape; the same message then writes a TERMINAL without the cell, and the
golden assertion reads red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.remote_lane_close import ModelRemoteLaneResult
from omnimarket.nodes.node_remote_lane_close_compute.handlers.handler_remote_lane_result import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    cells,
    request,
    stream_result,
)

pytestmark = [pytest.mark.unit]

DELEGATION = "DELEGATION delegated=0 reason=read-only-lane"
BLOCKED_RERUN = (
    "The required check failed on a runner timeout, not on the change.\n"
    "BLOCKER: rerun the check on the exact head\n"
    "LANE_RESULT outcome=blocked needs=rerun:omnibase_core#2302\n" + DELEGATION
)
BLOCKED_NO_CELL = (
    "I could not push; someone must rerun the check.\n"
    "BLOCKER: rerun needed\nLANE_RESULT outcome=blocked\n" + DELEGATION
)


class UnfixedClose(HandlerRemoteLaneResult):
    """The injected break ``drop-forwarding``: the close result keeps no cell beyond the ones the unfixed runner copied."""

    def handle(self, request):  # type: ignore[no-untyped-def]
        decided = super().handle(request)
        kept = {
            k: v
            for k, v in decided.ledger_cells.items()
            if k not in ("needs", "report")
        }
        return ModelRemoteLaneResult.model_validate(
            decided.model_dump()
            | {"ledger_cells": kept, "needs": None, "report_text": ""}
        )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    close: HandlerRemoteLaneResult | None = None,
) -> tuple[Rig, str]:
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(exit_polls=1, log_lines=(stream_result(message),)),
    )
    if close is not None:
        # the injected break replaces only the close node
        monkeypatch.setattr(rig.orchestrator, "_close", close)
    rig.orchestrator.run(request())
    [terminal] = rig.ledger_rows("TERMINAL")
    return rig, terminal


def _assert_carries_the_ask(terminal: str) -> None:
    found = cells(terminal)
    assert found["needs"] == "rerun:omnibase_core#2302"
    assert "runner timeout" in found["report"]
    assert found["outcome"] == "blocked"


def test_golden_a_blocked_lane_naming_a_rerun_writes_the_cell_and_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, terminal = _run(tmp_path, monkeypatch, BLOCKED_RERUN)
    _assert_carries_the_ask(terminal)
    assert all(not grammar.row_refusals(row) for row in rig.executor.attempts), [
        grammar.row_refusals(row) for row in rig.executor.attempts
    ]


def test_error_a_blocked_lane_with_no_cell_writes_needs_unparsed_and_keeps_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, terminal = _run(tmp_path, monkeypatch, BLOCKED_NO_CELL)
    found = cells(terminal)
    assert found["needs"] == "unparsed"
    assert "someone must rerun the check" in found["report"]
    assert found["outcome"] == "blocked"
    assert all(not grammar.row_refusals(row) for row in rig.executor.attempts)


def test_golden_a_handed_off_lane_writes_no_needs_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, terminal = _run(
        tmp_path,
        monkeypatch,
        f"Opened omnimarket#1.\nLANE_RESULT outcome=handed-off\n{DELEGATION}",
    )
    assert "needs" not in cells(terminal)
    assert cells(terminal)["outcome"] == "handed-off"


def test_control_drop_forwarding_writes_a_row_without_the_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unfixed runner's shape: the same message, a TERMINAL with no needs cell and no report."""
    _, terminal = _run(tmp_path, monkeypatch, BLOCKED_RERUN, close=UnfixedClose())
    assert "needs" not in cells(terminal)
    assert "report" not in cells(terminal)
    with pytest.raises(KeyError):
        _assert_carries_the_ask(terminal)
