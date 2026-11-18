# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill tick shell entry: the golden and error chains (OMN-19998).

Drives tick_entry.main with JSON text on stdin: exit 0 with the typed result on stdout and the
step line on stderr; exit 2 for a request that does not validate; exit 3 for a port failure. An
owned row is skipped, not dispatched, through the CLI.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_tick import (
    HandlerLabFillTick,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.protocol_lab_fill_effect import (
    LabFillPortError,
)
from omnimarket.nodes.node_lab_fill_effect.tick_entry import main

from .test_golden_chain_lab_fill_tick import _owners, _request, _row, _tick

pytestmark = pytest.mark.unit


def _run(payload: str, **kwargs: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(io.StringIO(payload), out, err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def test_golden_tick_dispatches_both_rows(tmp_path: Path) -> None:
    handler, runner, _ = _tick([_row(1), _row(2)])
    code, out, err = _run(_request(tmp_path).model_dump_json(), ticker=handler)
    assert code == 0
    result = json.loads(out)
    assert result["outcome"] == "dispatched"
    assert [(d["ticket"], d["host"]) for d in result["dispatched"]] == [
        ("OMN-1", "h201"),
        ("OMN-2", "h201"),
    ]
    assert len(runner.calls) == 2
    assert "step=run_lab_fill_tick outcome=dispatched free_slots=2 placed=2" in err


def test_an_owned_row_is_skipped_not_dispatched(tmp_path: Path) -> None:
    owners = _owners({"OMN-1": {"state": "held", "lane": "other-lane"}})
    handler, runner, _ = _tick([_row(1), _row(2)], owners=owners)
    code, out, _ = _run(_request(tmp_path).model_dump_json(), ticker=handler)
    assert code == 0
    result = json.loads(out)
    assert [d["ticket"] for d in result["dispatched"]] == ["OMN-2"]
    assert [(s["ticket"], s["reason"]) for s in result["skipped"]] == [
        ("OMN-1", "owned:other-lane")
    ]
    assert len(runner.calls) == 1


def test_an_invalid_request_is_exit_2_with_no_stdout() -> None:
    code, out, err = _run('{"hosts": []}')
    assert code == 2
    assert out == ""
    assert "invalid request" in err


def test_a_port_failure_is_exit_3_with_the_error_named(tmp_path: Path) -> None:
    class BrokenTicker(HandlerLabFillTick):
        def handle(self, request: Any) -> Any:
            raise LabFillPortError("RUNNER unreadable")

    code, out, err = _run(_request(tmp_path).model_dump_json(), ticker=BrokenTicker())
    assert code == 3
    assert out == ""
    assert "RUNNER unreadable" in err
