# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the record step with the retired script on 20 recorded ticks (OMN-23734, AC2).

``fixtures/recorded_record_ticks.json`` holds 20 hourly ticks recorded by running the script's own
``write_heartbeat``, ``record_open_count``, ``record_repo_status`` and the goal record's write into one
state directory: each tick's inputs and the content of every row file after it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_record_effect.handlers.handler_throughput_tick_record import (
    GOAL_STATE_FILE,
    HEARTBEAT_FILE,
    HISTORY_FILE,
    REPO_STATUS_EVENTS,
    REPO_STATUS_FILE,
    STATE_DIR_FIX,
    HandlerThroughputTickRecord,
)
from omnimarket.nodes.node_throughput_tick_record_effect.handlers.tick_record_ports import (
    InMemoryTickStateStore,
)
from omnimarket.nodes.node_throughput_tick_record_effect.models.model_throughput_tick_record import (
    ModelThroughputTickRecordRequest,
)

_TICKS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "recorded_record_ticks.json").read_text(
        encoding="utf-8"
    )
)["ticks"]
_ALL = [
    HEARTBEAT_FILE,
    HISTORY_FILE,
    REPO_STATUS_FILE,
    REPO_STATUS_EVENTS,
    GOAL_STATE_FILE,
]


def _request(tick: dict[str, Any], state_dir: str) -> ModelThroughputTickRecordRequest:
    return ModelThroughputTickRecordRequest.model_validate(
        {**tick["request"], "state_dir": state_dir}
    )


def test_the_recording_holds_twenty_ticks() -> None:
    assert len(_TICKS) == 20


def test_heartbeat_open_count_and_repo_status_rows_match_the_script_on_twenty_ticks_parity(
    tmp_path: Path,
) -> None:
    handler = HandlerThroughputTickRecord()
    for index, tick in enumerate(_TICKS):
        result = handler.handle(_request(tick, str(tmp_path)))
        assert result.ok, (index, result)
        for name in _ALL:
            expected = tick["files_after"].get(name)
            target = tmp_path / name
            assert (
                target.read_text(encoding="utf-8") if target.exists() else None
            ) == expected, (index, name)


def test_the_in_memory_store_keeps_the_same_rows_as_the_file_store_parity() -> None:
    store = InMemoryTickStateStore()
    handler = HandlerThroughputTickRecord(store)
    for tick in _TICKS:
        handler.handle(_request(tick, "/state"))
        for name, text in tick["files_after"].items():
            assert store.rows[name] == text


def test_golden_one_tick_writes_the_five_rows() -> None:
    store = InMemoryTickStateStore()
    result = HandlerThroughputTickRecord(store).handle(
        ModelThroughputTickRecordRequest.model_validate(
            {
                "state_dir": "/state",
                "heartbeat": {"stamp": "2026-11-14T05:00:00Z", "pid": 7},
                "open_count": {
                    "history": [{"at": "2026-11-14T04:00:00Z", "open": 9}],
                    "at": "2026-11-14T05:00:00Z",
                    "count": 11,
                    "basis": "b1",
                },
                "repo_status": {
                    "verdicts": [
                        {
                            "repo": "omnimarket",
                            "opened_at": "2026-11-14T04:40:00Z",
                            "verdict": "waiting_on_ci",
                            "event": True,
                        }
                    ],
                    "at": "2026-11-14T05:00:00Z",
                },
                "goal_record": {"at": "2026-11-14T05:00:00Z", "merges": 2},
            }
        )
    )
    assert result.ok
    assert result.written == [
        HEARTBEAT_FILE,
        HISTORY_FILE,
        REPO_STATUS_EVENTS,
        REPO_STATUS_FILE,
        GOAL_STATE_FILE,
    ]
    assert store.rows[HEARTBEAT_FILE] == "2026-11-14T05:00:00Z pid=7\n"
    assert store.rows[HISTORY_FILE] == (
        '{"history": [{"at": "2026-11-14T04:00:00Z", "open": 9, "basis": "b1"}, {"at": "2026-11-14T05:00:00Z", "open": 11, "basis": "b1"}]}\n'
    )
    assert (
        store.rows[REPO_STATUS_FILE]
        == '{"open_units": [{"repo": "omnimarket", "opened_at": "2026-11-14T04:40:00Z", "verdict": "waiting_on_ci"}]}\n'
    )
    assert store.rows[REPO_STATUS_EVENTS] == (
        '{"kind": "repo-status-verdict", "at": "2026-11-14T05:00:00Z", "repo": "omnimarket", "opened_at": "2026-11-14T04:40:00Z", "verdict": "waiting_on_ci", "event": true}\n'
    )
    assert (
        store.rows[GOAL_STATE_FILE] == '{"at": "2026-11-14T05:00:00Z", "merges": 2}\n'
    )


def test_the_history_keeps_twelve_points() -> None:
    store = InMemoryTickStateStore()
    history = [{"at": f"2026-11-14T{h:02d}:00:00Z", "open": h} for h in range(13)]
    HandlerThroughputTickRecord(store).handle(
        ModelThroughputTickRecordRequest.model_validate(
            {
                "state_dir": "/s",
                "open_count": {
                    "history": history,
                    "at": "2026-11-14T13:00:00Z",
                    "count": 99,
                    "basis": "b",
                },
            }
        )
    )
    points = json.loads(store.rows[HISTORY_FILE])["history"]
    assert len(points) == 12
    assert points[-1]["open"] == 99
    assert points[0]["open"] == 2


def test_a_planted_change_to_one_input_turns_the_parity_red(tmp_path: Path) -> None:
    tick = copy.deepcopy(_TICKS[0])
    tick["request"]["open_count"]["count"] += 1
    HandlerThroughputTickRecord().handle(_request(tick, str(tmp_path)))
    assert (tmp_path / HISTORY_FILE).read_text(encoding="utf-8") != _TICKS[0][
        "files_after"
    ][HISTORY_FILE]


def test_error_chain_each_unwritable_row_is_a_finding_and_the_others_still_land() -> (
    None
):
    store = InMemoryTickStateStore(
        fail={
            HEARTBEAT_FILE: "Read-only file system",
            HISTORY_FILE: "No space left on device",
            REPO_STATUS_EVENTS: "Permission denied",
            GOAL_STATE_FILE: "Disk quota exceeded",
        }
    )
    request = copy.deepcopy(_TICKS[0]["request"])
    request["repo_status"] = {
        "verdicts": [
            {
                "repo": "omnimarket",
                "opened_at": "2026-11-14T04:40:00Z",
                "verdict": "x",
                "event": True,
            }
        ],
        "at": "2026-11-14T05:00:00Z",
    }
    result = HandlerThroughputTickRecord(store).handle(
        ModelThroughputTickRecordRequest.model_validate(
            {**request, "state_dir": "/state"}
        )
    )
    assert result.ok is False
    assert (
        result.heartbeat_write_error
        == f"/state/{HEARTBEAT_FILE}: Read-only file system"
    )
    assert [(f.level, f.loop, f.why, f.fix) for f in result.findings] == [
        (
            "unknown",
            "open-trend-history",
            f"cannot write /state/{HISTORY_FILE}: No space left on device",
            STATE_DIR_FIX,
        ),
        (
            "unknown",
            "repo-status-units",
            f"cannot write /state/{REPO_STATUS_FILE}: Permission denied",
            STATE_DIR_FIX,
        ),
        (
            "unknown",
            "goal-check-record",
            f"cannot write /state/{GOAL_STATE_FILE}: Disk quota exceeded",
            STATE_DIR_FIX,
        ),
    ]
    assert result.written == []
    assert (
        REPO_STATUS_FILE not in store.rows
    )  # the events write failed first, as in the script


def test_error_chain_an_unmakeable_state_directory_is_the_finding_not_a_raise(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    result = HandlerThroughputTickRecord().handle(
        _request(_TICKS[0], str(blocker / "state"))
    )
    assert result.ok is False
    assert result.heartbeat_write_error is not None
    assert result.heartbeat_write_error.startswith(
        str(blocker / "state" / HEARTBEAT_FILE)
    )
    assert {f.loop for f in result.findings} == {
        "open-trend-history",
        "repo-status-units",
        "goal-check-record",
    }


def test_a_verdict_that_names_no_repo_is_refused_at_the_model() -> None:
    with pytest.raises(ValueError, match="repo"):
        ModelThroughputTickRecordRequest.model_validate(
            {
                "state_dir": "/s",
                "repo_status": {
                    "verdicts": [{"opened_at": "2026-11-14T04:40:00Z", "verdict": "x"}],
                    "at": "2026-11-14T05:00:00Z",
                },
            }
        )
