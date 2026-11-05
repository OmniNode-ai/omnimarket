# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The detached parent's placement read is a typed outcome, not a bare exit code (OMN-19998).

Recorded case: runner logs carried 550 lines of the form
``PLACEMENT lane=<lane> status=placing after 30s; read it with: onex-remote-lane wait --receipt <path>``
with exit 3, which scripts read as an error. The lane was not failed: it was still being placed.
The node now returns one of placed, placing, refused or crashed as typed JSON beside the line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lane_placement_recovery_effect.models import (
    ModelPlacementWaitResult,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
    NodeLanePlacementRecoveryEffect,
)

pytestmark = pytest.mark.unit

RUNTIME: dict[str, Any] = {
    "EXIT_OK": 0,
    "EXIT_NO_HOST": 4,
    "EXIT_LANE_ERROR": 1,
    "EXIT_REMOTE_FAILED": 70,
    "EXIT_STILL_RUNNING": 3,
}


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(s, 1.0)


def _wait(
    tmp_path: Path, receipt: dict[str, Any] | None, *, alive: bool = True
) -> tuple[ModelPlacementWaitResult, list[str]]:
    path = tmp_path / "rlane-x.json"
    if receipt is not None:
        path.write_text(json.dumps(receipt), encoding="utf-8")
    lines: list[str] = []
    clock = _Clock()
    result = NodeLanePlacementRecoveryEffect(RUNTIME).placement_wait_result(
        path,
        30.0,
        lane="lane-x",
        alive=lambda: alive,
        echo=lines.append,
        clock=clock,
        sleep=clock.sleep,
    )
    return result, lines


def _json_line(lines: list[str]) -> dict[str, Any]:
    tagged = [line for line in lines if line.startswith("PLACEMENT_RESULT ")]
    assert len(tagged) == 1, lines
    data = json.loads(tagged[0].removeprefix("PLACEMENT_RESULT "))
    assert isinstance(data, dict)
    return data


def test_recorded_placing_after_30s_is_typed_placing_not_an_error(
    tmp_path: Path,
) -> None:
    result, lines = _wait(tmp_path, {"status": "placing"})
    assert result.outcome == "placing"
    assert result.error is False
    assert result.exit_code == 3
    # The human line is unchanged for readers of the old text.
    assert lines[0].startswith("PLACEMENT lane=lane-x status=placing after 30s;")
    data = _json_line(lines)
    assert data["outcome"] == "placing"
    assert data["error"] is False
    assert data["wait_command"].startswith("onex-remote-lane wait --receipt ")
    assert ModelPlacementWaitResult(**data) == result


@pytest.mark.parametrize(
    ("receipt", "alive", "outcome", "error", "code"),
    [
        ({"status": "running", "host": "h1"}, True, "placed", False, 0),
        ({"status": "preparing", "host": "h1"}, True, "placed", False, 0),
        ({"status": "done", "final": True, "host": "h1"}, True, "placed", False, 0),
        (
            {"status": "no-host", "final": True, "reason": "ADMISSION-REFUSED"},
            True,
            "refused",
            True,
            4,
        ),
        (
            {"status": "failed", "final": True, "exit_code": 9, "error": "boom"},
            True,
            "crashed",
            True,
            9,
        ),
        ({"status": "placing"}, False, "crashed", True, 70),
        (None, False, "crashed", True, 70),
    ],
)
def test_every_terminal_read_is_one_typed_outcome(
    tmp_path: Path,
    receipt: dict[str, Any] | None,
    alive: bool,
    outcome: str,
    error: bool,
    code: int,
) -> None:
    result, lines = _wait(tmp_path, receipt, alive=alive)
    assert (result.outcome, result.error, result.exit_code) == (outcome, error, code)
    assert _json_line(lines)["outcome"] == outcome


def test_placement_wait_keeps_its_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "rlane-y.json"
    path.write_text(json.dumps({"status": "placing"}), encoding="utf-8")
    clock = _Clock()
    lines: list[str] = []
    code = NodeLanePlacementRecoveryEffect(RUNTIME).placement_wait(
        path,
        30.0,
        lane="lane-y",
        alive=lambda: True,
        echo=lines.append,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 3
    assert _json_line(lines)["outcome"] == "placing"


def test_recorded_wait_uses_canonical_typed_handler(tmp_path: Path) -> None:
    from omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_placement_wait import (
        HandlerPlacementWait,
    )
    from omnimarket.nodes.node_lane_placement_recovery_effect.models.model_placement_wait import (
        ModelPlacementWaitRequest,
    )

    path = tmp_path / "recorded-placing.json"
    path.write_text(json.dumps({"status": "placing"}), encoding="utf-8")
    clock = _Clock()
    lines: list[str] = []
    result = HandlerPlacementWait(RUNTIME).handle(
        ModelPlacementWaitRequest(
            receipt=path,
            max_s=30.0,
            lane="lane-x",
            alive=lambda: True,
            echo=lines.append,
            clock=clock,
            sleep=clock.sleep,
        )
    )
    assert result.outcome == "placing"
    assert result.error is False
    assert result.exit_code == 3
    assert clock.t == 30.0
    assert lines[0].startswith("PLACEMENT lane=lane-x status=placing after 30s;")
    assert ModelPlacementWaitResult(**_json_line(lines)) == result


def test_node_dispatches_typed_wait_request(tmp_path: Path) -> None:
    from dataclasses import FrozenInstanceError
    from typing import get_type_hints

    from omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_placement_wait import (
        HandlerPlacementWait,
    )
    from omnimarket.nodes.node_lane_placement_recovery_effect.models import (
        ModelPlacementWaitRequest,
    )

    path = tmp_path / "dispatch.json"
    path.write_text(json.dumps({"status": "running", "host": "h1"}), encoding="utf-8")
    lines: list[str] = []
    request = ModelPlacementWaitRequest(
        path, 30.0, "lane-x", lambda: True, echo=lines.append
    )
    hints = get_type_hints(HandlerPlacementWait.handle)
    assert hints["request"] is ModelPlacementWaitRequest
    assert hints["return"] is ModelPlacementWaitResult
    with pytest.raises(FrozenInstanceError):
        request.lane = "changed"
    result = NodeLanePlacementRecoveryEffect(RUNTIME).handle(request)
    assert isinstance(result, ModelPlacementWaitResult)
    assert result.outcome == "placed"
    assert _json_line(lines)["host"] == "h1"


@pytest.mark.parametrize("contents", ["{partial", "[]", "null", b"\xff"])
def test_unreadable_receipt_is_still_placing(
    tmp_path: Path, contents: str | bytes
) -> None:
    path = tmp_path / "rlane-x.json"
    path.write_bytes(contents.encode() if isinstance(contents, str) else contents)
    result, lines = _wait(tmp_path, None)
    assert result.outcome == "placing"
    assert result.error is False
    assert _json_line(lines)["exit_code"] == 3


@pytest.mark.parametrize("code", [None, 0, False, "9"])
def test_failed_receipt_without_integer_error_uses_lane_error(
    tmp_path: Path, code: Any
) -> None:
    result, lines = _wait(
        tmp_path,
        {
            "status": "failed",
            "final": True,
            "exit_code": code,
            "failure": {"reason": "line1\nline2"},
        },
    )
    assert result.exit_code == RUNTIME["EXIT_LANE_ERROR"]
    assert result.reason == "line1 line2"
    assert _json_line(lines)["outcome"] == "crashed"


def test_runner_final_receipt_is_reread_before_declaring_dead(tmp_path: Path) -> None:
    path = tmp_path / "race.json"
    path.write_text(json.dumps({"status": "placing"}), encoding="utf-8")
    lines: list[str] = []

    def exited() -> bool:
        path.write_text(
            json.dumps({"status": "done", "final": True, "host": "h1"}),
            encoding="utf-8",
        )
        return False

    code = NodeLanePlacementRecoveryEffect(RUNTIME).placement_wait(
        path, 30.0, lane="lane-x", alive=exited, echo=lines.append
    )
    assert code == 0
    assert _json_line(lines)["outcome"] == "placed"
