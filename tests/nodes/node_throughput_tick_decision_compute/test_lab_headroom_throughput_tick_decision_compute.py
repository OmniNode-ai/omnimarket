# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20686: the lab-headroom finding and the tick heartbeat finding of the throughput tick decisions.

Parity: tests/fixtures/throughput_tick_cli_parity.json holds cases captured by running the retired
throughput_tick.py end to end (fake launchctl and ps, a controller state directory, the PR watcher's
state, a lab pool with limited and auth-expired marks, runner receipts and live placement markers), beside
the request the repointed tick built from the same fixtures. The node's finding lines, status line and
exit code must equal what the retired script printed. Direct cases pin one rule each.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PARITY = json.loads(
    (ROOT / "tests/fixtures/throughput_tick_cli_parity.json").read_text()
)
HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-09T06:00:00Z"
READING = "h201:cores=32,load1=4.0,free_gb=64.0,placed=1,cap=4,slots=3,login=claude"
HOST = {"name": "h201", "parses": {READING: {"parsed": True}}}


def run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "now": NOW,
        "launchctl": "not_loaded",
        "lab_headroom": {"hosts": [HOST], "receipts": [_receipt()]},
        **overrides,
    }
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def _receipt(**overrides: Any) -> dict[str, Any]:
    return {
        "started_at": "2026-10-09T05:59:00Z",
        "host": "h201",
        "status": "running",
        "pid_alive": True,
        "readings": [READING],
        **overrides,
    }


def lab_lines(result: Any) -> list[str]:
    return [line for line in result.lines if "lab-headroom" in line]


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: str(c["name"]))
def test_old_cli_parity(case: dict[str, Any]) -> None:
    result = HANDLER.handle(ModelThroughputTickRequest.model_validate(case["request"]))
    assert [*result.lines, result.status_line] == case["expected"]["output"]
    assert result.exit_code == case["expected"]["exit_code"]


def test_parity_fixture_exercises_every_lab_headroom_finding_class() -> None:
    lines = [
        re.sub(r"lab-headroom:h\d+", "lab-headroom:h201", line)
        for c in PARITY["cases"]
        for line in c["expected"]["output"]
    ]
    for needle in (
        "MISSING lab-headroom:",
        "NOTE lab-headroom:h201 | unavailable in host table; no dispatch",
        "NOTE lab-headroom:h201 | limited until",
        "NOTE lab-headroom:h201 | limited mark unreadable",
        "NOTE lab-headroom:h201 | claude auth expired since",
        "NOTE lab-headroom:h201 | auth-expired mark unreadable",
        "NOTE lab-headroom:h201 | no recent placement reading",
        "NOTE lab-headroom:h201 | unhealthy:",
        "NOTE lab-headroom:h201 | admission refused:",
        "NOTE lab-headroom:h201 | unreadable placement reading",
        "NOTE lab-headroom:h201 | incomplete placement reading",
        "NOTE lab-headroom:h201 | running lanes",
        "NOTE lab-headroom: cannot read placement state",
    ):
        assert any(line.startswith(needle) for line in lines), needle


def test_free_slots_are_a_missing_finding_naming_the_host() -> None:
    result = run()
    assert lab_lines(result) == [
        "MISSING lab-headroom:h201 | running lanes 1 of cap 4, free slots 3 | FIX: dispatch up to 3 lanes of "
        "the session's pillar work to h201 through the remote-lane runner; never pin to a host without headroom"
    ]
    assert result.checked[-1] == "lab-headroom"


def test_no_lab_facts_skip_the_finding_and_its_checked_entry() -> None:
    result = run(lab_headroom=None)
    assert not lab_lines(result)
    assert "lab-headroom" not in result.checked


def test_a_live_marker_and_a_running_receipt_are_one_lane_not_two() -> None:
    result = run(
        lab_headroom={
            "hosts": [HOST],
            "receipts": [_receipt()],
            "live_marker_hosts": ["h201"],
        }
    )
    assert "running lanes 1 of cap 4, free slots 3" in lab_lines(result)[0]
    two = run(
        lab_headroom={
            "hosts": [HOST],
            "receipts": [_receipt()],
            "live_marker_hosts": ["h201", "h201"],
        }
    )
    assert "running lanes 2 of cap 4, free slots 2" in lab_lines(two)[0]


def test_a_finished_or_dead_receipt_holds_no_slot() -> None:
    for gone in (
        _receipt(final=True),
        _receipt(pid_alive=False),
        _receipt(status="done"),
    ):
        result = run(lab_headroom={"hosts": [HOST], "receipts": [gone]})
        assert "running lanes 0 of cap 4, free slots 4" in lab_lines(result)[0]


def test_a_running_receipt_with_a_non_integer_pid_is_dropped_whole() -> None:
    result = run(
        lab_headroom={"hosts": [HOST], "receipts": [_receipt(pid_valid=False)]}
    )
    assert lab_lines(result) == [
        "NOTE lab-headroom:h201 | no recent placement reading; no dispatch"
    ]


def test_the_host_table_cap_bounds_the_reading() -> None:
    result = run(lab_headroom={"hosts": [{**HOST, "cap": 2}], "receipts": [_receipt()]})
    assert "running lanes 1 of cap 2, free slots 1" in lab_lines(result)[0]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"limited_mark": {"state": "present", "until": "2026-10-09T07:00:00Z"}},
            "NOTE lab-headroom:h201 | limited until 2026-10-09T07:00:00Z; no dispatch",
        ),
        (
            {"limited_mark": {"state": "present", "until": "2026-10-09T05:00:00Z"}},
            None,
        ),
        (
            {"limited_mark": {"state": "unreadable"}},
            "NOTE lab-headroom:h201 | limited mark unreadable; no dispatch",
        ),
        (
            {"limited_mark": {"state": "present", "until": "soon"}},
            "NOTE lab-headroom:h201 | limited mark unreadable; no dispatch",
        ),
        (
            {
                "auth_mark": {
                    "state": "present",
                    "until": "2026-10-12T06:00:00Z",
                    "at": "2026-10-09T04:00:00Z",
                }
            },
            "NOTE lab-headroom:h201 | claude auth expired since 2026-10-09T04:00:00Z; log in again; no dispatch",
        ),
        (
            {"auth_mark": {"state": "present", "until": "2026-10-12T06:00:00Z"}},
            "NOTE lab-headroom:h201 | auth-expired mark unreadable; no dispatch",
        ),
        (
            {
                "auth_mark": {
                    "state": "present",
                    "until": "2026-10-09T05:00:00Z",
                    "at": "2026-10-08T04:00:00Z",
                }
            },
            None,
        ),
    ],
)
def test_marks_decide_whether_a_host_takes_work(
    changes: dict[str, Any], expected: str | None
) -> None:
    result = run(
        lab_headroom={"hosts": [{**HOST, **changes}], "receipts": [_receipt()]}
    )
    if expected is None:
        assert lab_lines(result)[0].startswith("MISSING lab-headroom:h201")
    else:
        assert lab_lines(result) == [expected]


def test_a_stale_or_future_reading_is_no_reading() -> None:
    for started in ("2026-10-09T05:40:00Z", "2026-10-09T06:10:00Z"):
        result = run(
            lab_headroom={"hosts": [HOST], "receipts": [_receipt(started_at=started)]}
        )
        assert lab_lines(result) == [
            "NOTE lab-headroom:h201 | no recent placement reading; no dispatch"
        ]


def test_the_newest_reading_wins() -> None:
    older = "h201:cores=32,load1=4.0,free_gb=64.0,placed=0,cap=4,slots=0,login=claude"
    host = {**HOST, "parses": {READING: {"parsed": True}, older: {"parsed": True}}}
    result = run(
        lab_headroom={
            "hosts": [host],
            "receipts": [
                _receipt(
                    started_at="2026-10-09T05:55:00Z", status="done", readings=[older]
                ),
                _receipt(readings=[READING]),
            ],
        }
    )
    assert lab_lines(result)[0].startswith("MISSING lab-headroom:h201 | running lanes")
    assert "free slots 3" in lab_lines(result)[0]


def test_an_unhealthy_unparsed_or_refused_reading_is_a_note() -> None:
    bad = "h201:UNREADABLE(ssh timeout)"
    unhealthy = run(
        lab_headroom={"hosts": [HOST], "receipts": [_receipt(readings=[bad])]}
    )
    assert lab_lines(unhealthy) == [
        f"NOTE lab-headroom:h201 | unhealthy: {bad}; no dispatch"
    ]
    unparsed = run(
        lab_headroom={
            "hosts": [{"name": "h201", "parses": {READING: {"parsed": False}}}],
            "receipts": [_receipt()],
        }
    )
    assert lab_lines(unparsed) == [
        "NOTE lab-headroom:h201 | unreadable placement reading; no dispatch"
    ]
    refused = run(
        lab_headroom={
            "hosts": [
                {
                    "name": "h201",
                    "parses": {
                        READING: {"parsed": True, "admission_refusal": "LOAD-OVER-BAR"}
                    },
                }
            ],
            "receipts": [_receipt()],
        }
    )
    assert lab_lines(refused) == [
        "NOTE lab-headroom:h201 | admission refused: LOAD-OVER-BAR; running lanes 1 of cap 4, free slots 0"
    ]


@pytest.mark.parametrize("missing", ["cap", "slots", "placed"])
def test_a_reading_without_a_field_is_incomplete_never_free(missing: str) -> None:
    reading = ",".join(
        part for part in READING.split(",") if not part.startswith(f"{missing}=")
    )
    host = {"name": "h201", "parses": {reading: {"parsed": True}}}
    result = run(
        lab_headroom={"hosts": [host], "receipts": [_receipt(readings=[reading])]}
    )
    assert lab_lines(result) == [
        "NOTE lab-headroom:h201 | incomplete placement reading; no dispatch"
    ]


def test_local_hosts_and_unavailable_rows_and_unreadable_state() -> None:
    local = run(
        lab_headroom={
            "hosts": [{"name": "local", "local": True}],
            "unavailable_hosts": ["h202"],
        }
    )
    assert lab_lines(local) == [
        "NOTE lab-headroom:h202 | unavailable in host table; no dispatch"
    ]
    error = run(lab_headroom={"placement_error": "boom", "unavailable_hosts": ["h202"]})
    assert lab_lines(error) == [
        "NOTE lab-headroom:h202 | unavailable in host table; no dispatch",
        "NOTE lab-headroom: cannot read placement state (boom)",
    ]
    module = run(lab_headroom={"module_unavailable": True})
    assert lab_lines(module) == ["NOTE lab-headroom: placement module unavailable"]
    assert module.checked[-1] == "lab-headroom"


def test_a_heartbeat_that_cannot_be_written_is_the_first_unknown() -> None:
    result = run(
        heartbeat_write_error="/state/throughput-tick.heartbeat: read-only",
        lab_headroom=None,
    )
    assert result.lines[0] == (
        "UNKNOWN tick-heartbeat | cannot write /state/throughput-tick.heartbeat: read-only | FIX: "
        "make the state directory writable (OMNI_SESSION_START_STATE_DIR names another)"
    )
    assert result.unknown[0] == "tick-heartbeat"
    assert result.exit_code == 1


def test_a_healthy_lab_adds_its_checked_entry_to_the_ok_line() -> None:
    result = run(
        launchctl="loaded",
        launchctl_stdout='{ "PID" = 77; };',
        pid_etime="02:00",
        ticks=[
            {"tick": 5, "ts": "2026-10-09T05:55:00Z", "mode": "act", "status": "OK"}
        ],
        heartbeat_text="2026-10-09T05:55:00Z\n",
        watcher_path="/w.json",
        watcher_state={
            "last_tick": "2026-10-09T05:58:00Z",
            "operator": "op",
            "prs": {
                f"m{i}": {
                    "facts": {
                        "repo": "r/a",
                        "state": "MERGED",
                        "merged_at": f"2026-10-09T05:{10 + i}:00Z",
                    }
                }
                for i in range(6)
            },
        },
        floors_per_repo={"r/a": 1},
        lab_headroom={"hosts": [{"name": "h201", "parses": {}}]},
    )
    assert result.status_line == (
        "THROUGHPUT OK checked=controller,merges,floors,lab-headroom"
    )
    assert result.exit_code == 0
