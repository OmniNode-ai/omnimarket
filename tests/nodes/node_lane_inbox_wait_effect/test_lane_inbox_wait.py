# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, failing-control and error tests of the lane inbox wait (OMN-24459).

Golden: a MSG to the lane at the third poll of a 180 s budget ends the wait WOKEN at that poll and
no later, and the event chain ends in completed. Failing controls: a quiet inbox runs the full
budget WAITED with the expected poll count; rows that are for another lane, older than ``since``,
from the lane itself, or answered by an ACK or a RELEASE do not wake it, while a to=all MSG does.
Error: an inbox that fails every read is UNREADABLE with a failed chain event, a failure in the
middle of the wait is recorded and waited out, a budget above the overlay's bound is refused, the
runtime source maps a broken reader run to unreadable, and the CLI exits by outcome.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.lane_inbox_wait import (
    EnumInboxWaitOutcome,
    ModelLaneInboxWaitCommand,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.adapters.inmemory_adapters import (
    FakeClock,
    FakeSleeper,
    InMemoryInboxSource,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.adapters.runtime_adapters import (
    ReaderInboxSource,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.handlers.handler_lane_inbox_wait import (
    HandlerLaneInboxWait,
    WaitBudgetRefusedError,
    events_for,
    load_topics,
    parse_inbox_row,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.overlay import (
    ModelLaneInboxWaitOverlay,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.protocols import InboxUnreadableError
from omnimarket.nodes.node_lane_inbox_wait_effect.runtime_lane_inbox_wait import main

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_inbox_wait_effect"
)
_LANE = "lane-a"
_SINCE = "2026-11-14T10:00:00Z"
# A stamp the CLI's start stamp (the real now) is always before.
_FUTURE = "2099-01-01T00:04:00Z"


def _overlay() -> ModelLaneInboxWaitOverlay:
    return ModelLaneInboxWaitOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def _command(**over: object) -> ModelLaneInboxWaitCommand:
    base: dict[str, object] = {
        "correlation_id": "c-1",
        "lane": _LANE,
        "seconds": 180.0,
        "since": _SINCE,
    }
    base.update(over)
    return ModelLaneInboxWaitCommand(**base)


def _handler(source: InMemoryInboxSource) -> HandlerLaneInboxWait:
    clock = FakeClock()
    return HandlerLaneInboxWait(
        overlay=_overlay(), source=source, sleeper=FakeSleeper(clock), clock=clock
    )


def test_a_msg_to_the_lane_at_the_third_poll_ends_the_wait_woken_there() -> None:
    msg = (
        "2026-11-14T10:04:00Z | MSG | id=m-3 | to=lane-a | from=lane-b "
        "| the fill for lane-a is ready"
    )
    source = InMemoryInboxSource(answers=[(), (), (msg,)])
    result = _handler(source).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.WOKEN
    assert result.polls == 3
    assert result.waited_seconds == 60.0
    assert len(source.reads) == 3
    [row] = result.rows
    assert (row.stamp, row.row_type, row.row_id, row.sender) == (
        "2026-11-14T10:04:00Z",
        "MSG",
        "m-3",
        "lane-b",
    )
    assert row.text == "the fill for lane-a is ready"
    assert (result.total, result.read_failures) == (1, ())
    assert "1 binding row(s) over 3 poll(s)" in result.detail


def test_the_golden_chain_ends_in_the_completed_event() -> None:
    msg = (
        "2026-11-14T10:04:00Z | MSG | id=m-3 | to=lane-a | from=lane-b "
        "| the fill for lane-a is ready"
    )
    result = _handler(InMemoryInboxSource(answers=[(), (), (msg,)])).handle(_command())
    topics = load_topics()
    chain = events_for(result, topics)
    assert [topic for topic, _ in chain] == [
        topics["wait_started"],
        topics["wait_woken"],
        topics["wait_completed"],
    ]
    assert (
        topics["wait_completed"] == "onex.evt.omnimarket.lane-inbox-wait-completed.v1"
    )
    assert chain[-1][1]["outcome"] == "woken"


def test_a_quiet_inbox_runs_the_full_budget_and_is_waited() -> None:
    source = InMemoryInboxSource()
    result = _handler(source).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.WAITED
    # 180 s at a 30 s poll interval: reads at 0, 30, ..., 180 - the budget's end is read once
    # more, so a handler that returns early on anything fails here.
    assert result.polls == 7
    assert len(source.reads) == 7
    assert result.waited_seconds == 180.0
    assert (result.rows, result.total, result.read_failures) == ((), 0, ())


def test_rows_that_are_not_for_the_lane_do_not_wake_it() -> None:
    rows = (
        # for another lane
        "2026-11-14T10:01:00Z | MSG | id=m-1 | to=lane-z | from=lane-b | for another lane",
        # older than `since`
        "2026-11-14T09:59:59Z | MSG | id=m-2 | to=lane-a | from=lane-b | stale",
        # from the lane itself
        "2026-11-14T10:02:00Z | MSG | id=m-3 | to=lane-a | from=lane-a | self echo",
        # answered by an ACK of the lane
        "2026-11-14T10:03:00Z | MSG | id=m-4 | to=lane-a | from=lane-b | answered",
        "2026-11-14T10:03:30Z | ACK | lane=lane-a | re=m-4",
        # answered by a RELEASE
        "2026-11-14T10:05:00Z | MSG | id=m-5 | to=lane-a | from=lane-b | released",
        "2026-11-14T10:05:30Z | RELEASE | re=m-5",
    )
    result = _handler(InMemoryInboxSource(answers=[rows])).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.WAITED
    assert (result.total, result.rows) == (0, ())
    assert result.polls == 7


def test_a_msg_addressed_to_all_does_wake_it() -> None:
    msg = (
        "2026-11-14T10:04:00Z | MSG | id=m-6 | to=all | from=lane-b "
        "| a broadcast every lane reads"
    )
    result = _handler(InMemoryInboxSource(answers=[(msg,)])).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.WOKEN
    assert [row.row_id for row in result.rows] == ["m-6"]
    assert result.total == 1


def test_a_hold_with_a_skip_cell_only_wakes_the_lane_it_names() -> None:
    skipped = (
        "2026-11-14T10:04:00Z | HOLD | id=h-1 | to=all | surface=tick "
        "| not the lane's surface"
    )
    named = (
        "2026-11-14T10:05:00Z | HOLD | id=h-2 | to=lane-a | surface=tick "
        "| held for the lane"
    )
    result = _handler(InMemoryInboxSource(answers=[(skipped,), (named,)])).handle(
        _command()
    )
    assert result.outcome is EnumInboxWaitOutcome.WOKEN
    assert result.polls == 2
    assert (result.total, [row.row_id for row in result.rows]) == (1, ["h-2"])


def test_a_msg_with_the_stop_cell_ends_the_wait_stop() -> None:
    binding = (
        "2026-11-14T10:04:00Z | MSG | id=m-7 | to=lane-a | from=lane-b | plain binding"
    )
    stop = (
        "2026-11-14T10:04:10Z | MSG | id=m-8 | to=lane-a | from=lane-b "
        "| action=stop | stop requested"
    )
    result = _handler(InMemoryInboxSource(answers=[(binding, stop)])).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.STOP
    assert result.polls == 1
    assert result.total == 2
    assert [row.row_id for row in result.rows] == ["m-7", "m-8"]
    topics = load_topics()
    assert [topic for topic, _ in events_for(result, topics)] == [
        topics["wait_started"],
        topics["wait_woken"],
        topics["wait_woken"],
        topics["wait_completed"],
    ]


def test_a_source_that_fails_every_read_is_unreadable_with_a_failed_event() -> None:
    # The failure is scripted at every read index, so the wait cannot read its way
    # out of it: the first read already fails, and so would every later one.
    source = InMemoryInboxSource(
        failures=dict.fromkeys(range(64), "the ledger host refused the read")
    )
    result = _handler(source).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.UNREADABLE
    assert result.polls == 1
    assert result.read_failures == ("the ledger host refused the read",)
    assert result.rows == ()
    topics = load_topics()
    chain = events_for(result, topics)
    assert [topic for topic, _ in chain] == [
        topics["wait_started"],
        topics["wait_failed"],
    ]
    assert chain[-1][1]["outcome"] == "unreadable"


def test_one_failure_in_the_middle_is_recorded_and_the_wait_goes_on() -> None:
    msg = (
        "2026-11-14T10:04:00Z | MSG | id=m-3 | to=lane-a | from=lane-b "
        "| the fill for lane-a is ready"
    )
    source = InMemoryInboxSource(
        answers=[(), (), (msg,)], failures={1: "one read timed out"}
    )
    result = _handler(source).handle(_command())
    assert result.outcome is EnumInboxWaitOutcome.WOKEN
    assert result.polls == 3
    assert result.read_failures == ("one read timed out",)
    assert result.rows[0].row_id == "m-3"


def test_a_budget_above_max_wait_seconds_is_refused() -> None:
    handler = _handler(InMemoryInboxSource())
    with pytest.raises(WaitBudgetRefusedError, match="max_wait_seconds"):
        handler.handle(_command(seconds=_overlay().max_wait_seconds + 1))


def test_the_runtime_reader_runs_the_overlay_reader_module_for_the_lane() -> None:
    reader = _overlay().reader
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, "2026-11-14T10:04:00Z | MSG\n\n", ""
        )

    source = ReaderInboxSource(reader, {reader.interpreter_env: "python3 -I"}, run=run)
    lines = source.read(lane=_LANE)
    assert lines == ("2026-11-14T10:04:00Z | MSG",)
    assert calls == [
        [
            "python3",
            "-m",
            reader.reader_module,
            "--reader",
            reader.reader_name,
            "--query",
            "rows",
            "--window-hours",
            str(reader.window_hours),
            "--lane",
            _LANE,
        ]
    ]


def _run_exiting(returncode: int) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return run


def _run_raising(error: Exception) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    return run


@pytest.mark.parametrize(
    ("interpreter", "run", "match"),
    [
        pytest.param(None, _run_exiting(0), "is not set", id="missing-variable"),
        pytest.param(
            '"python3', _run_exiting(0), "does not split", id="unparsable-variable"
        ),
        pytest.param(
            "python3",
            _run_raising(FileNotFoundError("gone")),
            "could not run",
            id="os-error",
        ),
        pytest.param("python3", _run_exiting(3), "exited 3", id="non-ok-exit"),
        pytest.param(
            "python3",
            _run_raising(subprocess.TimeoutExpired(cmd="python3", timeout=60)),
            "timed out",
            id="timeout",
        ),
    ],
)
def test_the_runtime_reader_maps_a_broken_run_to_unreadable(
    interpreter: str | None,
    run: Callable[..., subprocess.CompletedProcess[str]],
    match: str,
) -> None:
    reader = _overlay().reader
    environ = {} if interpreter is None else {reader.interpreter_env: interpreter}
    source = ReaderInboxSource(reader, environ, run=run)
    with pytest.raises(InboxUnreadableError, match=match):
        source.read(lane=_LANE)


def _overlay_environ(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "overlay-roots"
    node = root / "node_lane_inbox_wait_effect"
    node.mkdir(parents=True)
    (node / "overlay.yaml").write_text(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.mark.parametrize(
    ("source", "code", "outcome"),
    [
        pytest.param(InMemoryInboxSource(), 0, "waited", id="waited"),
        pytest.param(
            InMemoryInboxSource(
                answers=[(f"{_FUTURE} | MSG | id=m-1 | to=lane-a | the fill is ready",)]
            ),
            10,
            "woken",
            id="woken",
        ),
        pytest.param(
            InMemoryInboxSource(
                answers=[
                    (
                        f"{_FUTURE} | MSG | id=m-2 | to=lane-a | action=stop "
                        "| stop requested",
                    )
                ]
            ),
            11,
            "stop",
            id="stop",
        ),
        pytest.param(
            InMemoryInboxSource(failures={0: "the ledger host refused"}),
            75,
            "unreadable",
            id="unreadable",
        ),
    ],
)
def test_main_returns_the_exit_code_of_its_outcome(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source: InMemoryInboxSource,
    code: int,
    outcome: str,
) -> None:
    argv = ["--lane", _LANE, "--seconds", "180"]
    exit_code = main(argv, environ=_overlay_environ(tmp_path), handler=_handler(source))
    assert exit_code == code
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    printed = json.loads(lines[0])
    assert printed["outcome"] == outcome
    assert printed["lane"] == _LANE
    assert printed["correlation_id"].startswith("lane-inbox-wait-")


def test_the_overlay_refuses_a_missing_key() -> None:
    body = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del body["max_wait_seconds"]
    with pytest.raises(ValidationError):
        ModelLaneInboxWaitOverlay.model_validate(body)


def test_the_overlay_refuses_a_poll_interval_above_the_max_wait() -> None:
    body = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    body["poll_interval_seconds"] = body["max_wait_seconds"] + 1
    with pytest.raises(ValidationError):
        ModelLaneInboxWaitOverlay.model_validate(body)


def test_a_row_parses_its_cells_first_occurrence_winning_and_free_text() -> None:
    parsed = parse_inbox_row(
        "MSG 4471: 2026-11-14T10:04:00Z | MSG | id=m-9 | id=m-0 | to=lane-a "
        "| not a cell | from=lane-b"
    )
    assert parsed is not None
    assert (parsed.stamp, parsed.row_type) == ("2026-11-14T10:04:00Z", "MSG")
    assert parsed.cells["id"] == "m-9"  # first occurrence wins
    assert parsed.cells["to"] == "lane-a"
    assert parsed.cells["from"] == "lane-b"
    assert parsed.text == "not a cell"


def test_a_line_without_a_stamp_is_not_a_row() -> None:
    assert parse_inbox_row("") is None
    assert parse_inbox_row("not a ledger row at all") is None
