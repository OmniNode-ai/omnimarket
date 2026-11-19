# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill select shell entry: the golden and error chains (OMN-19998).

Drives select_entry.main with JSON text on stdin: exit 0 with the typed result on stdout and the
step line on stderr; exit 2 for a request that does not validate; exit 3 for a port failure; exit
69 for a not-ok ledger read (the typed result still on stdout); exit 78 when the reader overlay
cannot be loaded.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_select import (
    HandlerLabFillSelect,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.protocol_lab_fill_effect import (
    LabFillPortError,
)
from omnimarket.nodes.node_lab_fill_effect.select_entry import main

from .test_lab_fill_select import HANDOFF_ROW, FakeLedgerRows, select_request

pytestmark = pytest.mark.unit


def _run(payload: str, **kwargs: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(io.StringIO(payload), out, err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def test_golden_select_writes_the_result_and_the_step_line() -> None:
    code, out, err = _run(
        select_request().model_dump_json(),
        selector=HandlerLabFillSelect(FakeLedgerRows(lines=(HANDOFF_ROW,))),
    )
    assert code == 0
    result = json.loads(out)
    assert result["ledger_status"] == "ok"
    assert result["ledger_rows"] == 1
    assert [
        (d["ticket"], d["reason"], d["caller_reason"])
        for d in result["selection"]["decisions"]
    ] == [
        ("OMN-20677", "handed-off", "owned:handed-off:repo_a#235"),
        ("OMN-9999", None, ""),
    ]
    assert (
        "step=select_lab_fill_candidates ledger=ok rows=1 decisions=2"
        in err.splitlines()[0]
    )


def test_an_invalid_request_is_exit_2_with_no_stdout() -> None:
    code, out, err = _run('{"ledger_reader": "x"}')
    assert code == 2
    assert out == ""
    assert "invalid request" in err


def test_a_selection_that_fails_validation_is_exit_2() -> None:
    code, out, err = _run(
        json.dumps(
            {
                "selection": {},
                "ledger_reader": "lab-fill-selection",
                "ledger_window_hours": 27,
            }
        ),
        selector=HandlerLabFillSelect(FakeLedgerRows(lines=(HANDOFF_ROW,))),
    )
    assert code == 2
    assert out == ""
    assert "invalid request" in err


def test_an_unreadable_ledger_is_exit_69_with_the_typed_result_on_stdout() -> None:
    code, out, err = _run(
        select_request().model_dump_json(),
        selector=HandlerLabFillSelect(
            FakeLedgerRows(status="unreadable", reason="bus down")
        ),
    )
    assert code == 69
    result = json.loads(out)
    assert result["ledger_status"] == "unreadable"
    assert result["selection"] is None
    assert "ledger=unreadable" in err


def test_an_ok_read_of_the_same_request_is_exit_0() -> None:
    code, _, _ = _run(
        select_request().model_dump_json(),
        selector=HandlerLabFillSelect(FakeLedgerRows(lines=(HANDOFF_ROW,))),
    )
    assert code == 0


def test_a_port_failure_is_exit_3_with_the_error_named() -> None:
    class BrokenSelector(HandlerLabFillSelect):
        def handle(self, request: Any) -> Any:
            raise LabFillPortError("BUS down")

    code, out, err = _run(
        select_request().model_dump_json(),
        selector=BrokenSelector(FakeLedgerRows(lines=(HANDOFF_ROW,))),
    )
    assert code == 3
    assert out == ""
    assert "BUS down" in err


def test_a_missing_overlay_is_exit_78_and_refused(tmp_path: Path) -> None:
    code, out, err = _run(
        select_request().model_dump_json(),
        environ={"ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "missing.yaml")},
    )
    assert code == 78
    assert out == ""
    assert err.startswith("lab-fill select: REFUSED")
