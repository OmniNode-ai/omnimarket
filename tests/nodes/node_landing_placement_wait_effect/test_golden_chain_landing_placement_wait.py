# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Placement timing and missing-result gate bounds through direct calls."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_landing_placement_wait_effect import (
    NodeLandingPlacementWaitEffect,
)
from omnimarket.nodes.node_landing_placement_wait_effect.handlers import (
    handler_landing_placement_wait as pw,
)
from omnimarket.nodes.node_landing_placement_wait_effect.models import (
    ModelLandingPlacementWaitRequest,
)

pytestmark = pytest.mark.unit
START = "2030-01-01T00:00:00Z"
DEADLINE = "2030-01-01T00:45:00Z"
NOW = "2030-01-01T01:00:00Z"


def worker(path: Path, placed: str | None, host: str | None = "host-a") -> None:
    (path / "lease.json").write_text(
        json.dumps({"argv": ["python3", "landing_worker_run.py"]})
    )
    if placed:
        placement = path / "placement.json"
        placement.write_text(json.dumps({"at": START, "host": host}))
        stamp = datetime.fromisoformat(placed.replace("Z", "+00:00")).timestamp()
        os.utime(placement, (stamp, stamp))


def run(path: Path, rc: int = 143, now: str = NOW, **lease: Any) -> str:
    return (
        NodeLandingPlacementWaitEffect()
        .handle(
            ModelLandingPlacementWaitRequest(
                worker_dir=path,
                lease={"dispatched_at": START, "deadline_at": DEADLINE, **lease},
                engine_rc=rc,
                now=now,
            )
        )
        .cause
    )


@pytest.mark.parametrize("placed", ["00:37:00", "00:30:00", "00:30:30", "00:23:00"])
@pytest.mark.parametrize("rc", [86, 137, 143])
def test_late_placement_at_deadline(tmp_path: Path, placed: str, rc: int) -> None:
    worker(tmp_path, f"2030-01-01T{placed}Z")
    assert run(tmp_path, rc) == "placement-wait"


@pytest.mark.parametrize("host", [None, "host-a"])
def test_never_placed(tmp_path: Path, host: str | None) -> None:
    worker(tmp_path, START if host is None else None, host)
    assert pw.placed_at_of(tmp_path, {}) is False
    assert run(tmp_path) == "placement-wait"


def test_nonplacing_worker_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "lease.json").write_text(json.dumps({"argv": ["sleep", "60"]}))
    assert pw.placed_at_of(tmp_path, {}) is None
    assert run(tmp_path) == ""


def test_prompt_proves_setup_ran_without_pool(tmp_path: Path) -> None:
    worker(tmp_path, None)
    (tmp_path / "prompt.txt").write_text("task")
    assert pw.placed_at_of(tmp_path, {}) is None
    assert run(tmp_path) == ""


@pytest.mark.parametrize(
    ("placed", "rc", "now"),
    [
        ("00:37:00", 98, NOW),
        ("00:37:00", 70, NOW),
        ("00:37:00", 143, "2030-01-01T00:44:00Z"),
        ("00:14:00", 143, NOW),
        ("00:07:00", 86, NOW),
        ("00:22:30", 143, NOW),
    ],
)
def test_engine_had_time_or_was_not_deadline_killed(
    tmp_path: Path, placed: str, rc: int, now: str
) -> None:
    worker(tmp_path, f"2030-01-01T{placed}Z")
    assert run(tmp_path, rc, now) == ""


def test_lease_placement_overrides_file_and_file_stamp(tmp_path: Path) -> None:
    worker(tmp_path, START)
    late = "2030-01-01T00:37:00Z"
    assert run(tmp_path, placed_at=late) == "placement-wait"
    assert pw.placed_at_of(tmp_path, {"placed_at": late}) == datetime(
        2030, 1, 1, 0, 37, tzinfo=UTC
    )
    (tmp_path / "placement.json").write_text(
        json.dumps({"host": "host-a", "placed_at": late})
    )
    assert run(tmp_path) == "placement-wait"


def test_unreadable_deadlines_and_placement(tmp_path: Path) -> None:
    assert pw.deadline_passed({}, NOW)
    assert pw.deadline_passed({"deadline_at": DEADLINE}, "bad")
    assert pw.deadline_passed({"deadline_at": DEADLINE}, DEADLINE)
    assert not pw.deadline_passed({"deadline_at": DEADLINE}, START)
    assert run(tmp_path, deadline_at="bad") == ""
    assert run(tmp_path, placed_at="bad") == ""


def test_default_lease_window(tmp_path: Path) -> None:
    worker(tmp_path, "2030-01-01T00:37:00Z")
    assert run(tmp_path, dispatched_at="") == "placement-wait"


def test_missing_result_gate_expires_at_boundary(tmp_path: Path) -> None:
    entry = {"head": "a" * 40, "input": "input", "lease_id": 1, "at": START}
    assert pw.NO_RESULT_GATE_HOURS == 2.0
    assert pw.settle_gate_records(
        {"example#1": dict(entry)}, tmp_path, "2030-01-01T01:59:59Z"
    )
    assert (
        pw.settle_gate_records(
            {"example#1": dict(entry)}, tmp_path, "2030-01-01T02:00:00Z"
        )
        == {}
    )
    assert (
        pw.settle_gate_records(
            {"example#1": dict(entry)}, tmp_path, "2030-01-02T00:00:00Z"
        )
        == {}
    )


def test_no_attempt_gate_is_not_bounded(tmp_path: Path) -> None:
    entry = {"kind": "waiting_ci", "at": START}
    assert pw.settle_gate_records(
        {"example#1": entry}, tmp_path, "2030-01-02T00:00:00Z"
    ) == {"example#1": entry}


def test_gate_stamps_from_journal_else_now_and_preserves_existing(
    tmp_path: Path,
) -> None:
    (tmp_path / pw.FAILURES_LOG).write_text(
        json.dumps({"lease_id": 1, "ts": START}) + "\nnot json\n[]\n"
    )
    entries = {
        "example#1": {"lease_id": 1},
        "example#2": {"lease_id": 2},
        "example#3": {"lease_id": 3, "at": DEADLINE},
    }
    settled = pw.settle_gate_records(entries, tmp_path, NOW)
    assert {k: v["at"] for k, v in settled.items()} == {
        "example#1": START,
        "example#2": NOW,
        "example#3": DEADLINE,
    }
    assert (
        pw.settle_gate_records(
            {"example#1": {"lease_id": 1}}, tmp_path, "2030-01-02T00:00:00Z"
        )
        == {}
    )
