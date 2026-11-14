# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill send step as a shell entry: JSON request on stdin, launch result on stdout (OMN-23302).

The launchd lab-fill driver runs shell commands, so the node needs an entry it can call. These tests drive the entry
the way the driver does (the request as JSON text) over the same fakes the send tests use.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.__main__ import main
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
    HandlerLabFillLaunch,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_send import (
    HandlerLabFillSend,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillPortError

from .fakes import (
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeMerged,
    FakeRunner,
)
from .test_lab_fill_send import _request

pytestmark = pytest.mark.unit


def _sender(runner: FakeRunner, merged: FakeMerged | None = None) -> HandlerLabFillSend:
    return HandlerLabFillSend(
        HandlerLabFillLaunch(
            checks=FakeChecks(),
            approved=FakeApproved(),
            blocks=FakeBlocks(),
            runner=runner,
            clock=FakeClock(),
            merged=merged if merged is not None else FakeMerged(),
        )
    )


def _run(payload: str, sender: HandlerLabFillSend | None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(io.StringIO(payload), out, err, sender)
    return code, out.getvalue(), err.getvalue()


def _json(tmp_path: Path) -> str:
    return _request(tmp_path).model_dump_json()


def test_send_cli_launches_the_lane_and_names_the_step(tmp_path: Path) -> None:
    runner = FakeRunner()
    code, out, err = _run(_json(tmp_path), _sender(runner))
    assert code == 0
    result = json.loads(out)
    assert (result["lane"], result["detached"], result["skipped"]) == (
        "lab-fill-omn9-x",
        True,
        "",
    )
    assert "step=send_lab_fill_lane lane=lab-fill-omn9-x detached=true" in err
    assert len(runner.calls) == 1
    assert (
        (tmp_path / "lab-fill-omn9-x.md")
        .read_text(encoding="utf-8")
        .startswith("# Lane lab-fill-omn9-x")
    )


def test_merged_ticket_skip_through_the_send_cli_sends_no_lane(tmp_path: Path) -> None:
    runner = FakeRunner()
    merged = FakeMerged(by_ticket={"OMN-9": ["omnimarket#5400"]})
    code, out, err = _run(_json(tmp_path), _sender(runner, merged))
    assert code == 0
    result = json.loads(out)
    assert (result["detached"], result["skipped"]) == (False, "implementation-merged")
    assert result["merged_prs"] == ["omnimarket#5400"]
    assert "skipped=implementation-merged" in err
    assert runner.calls == []


def test_send_cli_refuses_a_request_that_does_not_validate() -> None:
    code, out, err = _run('{"brief_dir": "/b"}', None)
    assert code == 2
    assert out == ""
    assert "invalid request" in err


def test_send_cli_reports_a_port_failure_and_writes_no_result(tmp_path: Path) -> None:
    class _Failing(HandlerLabFillSend):
        def handle(self, request):  # type: ignore[no-untyped-def]
            raise LabFillPortError("RUNNER unreadable")

    code, out, err = _run(_json(tmp_path), _Failing())
    assert code == 3
    assert out == ""
    assert "RUNNER unreadable" in err
