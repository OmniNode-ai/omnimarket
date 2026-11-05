# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Portable cutover readbacks must establish order, parity and rollback."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_consumer_group_guard_effect import (
    runtime_consumer_group_guard_schedule as schedule_cli,
)
from omnimarket.nodes.node_consumer_group_guard_effect.guard_cutover import (
    verify_cutover,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 11, 3, 12, tzinfo=UTC)


def stamp(seconds: int) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def readback(seconds: int, *, exit_code: int = 0, stdout: str = "") -> dict[str, Any]:
    return {"observed_at": stamp(seconds), "exit_code": exit_code, "stdout": stdout}


def evidence() -> dict[str, Any]:
    """Synthetic readbacks; these are never a claim about a live host."""
    return {
        "host": "h053",
        "implementation_digests": {
            "mac_clone": "a" * 64,
            "lab_clone": "a" * 64,
            "lab_dispatch": "a" * 64,
        },
        "mac_unload": readback(0),
        "mac_absent_after_unload": readback(1, exit_code=113),
        "lab_install": readback(2),
        "host_reconcile": readback(3),
        "mac_absent_after_reconcile": readback(4, exit_code=113),
        "other_unit_name": "fixture-other-unit",
        "other_unit_install": readback(5),
        "mac_absent_after_other_install": readback(6, exit_code=113),
        "mac_baseline": {"broker": "fixture-broker:9092", "bus_lane": "dev"},
        "runs": [
            {
                "invocation_id": "a" * 32,
                "trigger": "timer",
                "started_at": stamp(10),
                "completed_at": stamp(12),
                "last_trigger_at": stamp(10),
                "observed_at": stamp(11),
                "status": "OK",
                "reap_detail": "reaped 3 of 3 eligible Empty groups",
                "broker": "fixture-broker:9092",
                "bus_lane": "dev",
            },
            {
                "invocation_id": "b" * 32,
                "trigger": "timer",
                "started_at": stamp(610),
                "completed_at": stamp(612),
                "last_trigger_at": stamp(610),
                "observed_at": stamp(611),
                "status": "OK",
                "reap_detail": "nothing to reap among 5 Empty groups",
                "broker": "fixture-broker:9092",
                "bus_lane": "dev",
            },
        ],
        "rollback_uninstall": readback(700),
        "rollback_stop": readback(701),
        "rollback_reset_failed": readback(702),
        "rollback_is_active": readback(703, exit_code=3, stdout="inactive\n"),
        "mac_reinstall": readback(704),
    }


def test_guard_cutover_complete_readbacks() -> None:
    result = verify_cutover(evidence(), host="h053", interval_seconds=300)
    assert result.exit_code == 0
    assert "readbacks satisfy" in result.stdout
    assert "h053" in result.stdout


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mac_unload", readback(3)),
        ("mac_unload", readback(0, exit_code=1)),
        ("mac_absent_after_unload", readback(3, exit_code=113)),
        ("mac_absent_after_unload", readback(1)),
        ("mac_absent_after_unload", readback(1, exit_code=-9)),
        ("host_reconcile", readback(3, exit_code=1)),
        ("mac_absent_after_reconcile", readback(2, exit_code=113)),
        ("mac_absent_after_reconcile", readback(4)),
        ("other_unit_install", readback(5, exit_code=1)),
        ("mac_absent_after_other_install", readback(4, exit_code=113)),
        ("mac_absent_after_other_install", readback(6)),
        ("mac_absent_after_other_install", readback(705, exit_code=113)),
        ("lab_install", readback(2, exit_code=1)),
        ("host", "other-host"),
        ("other_unit_name", "consumer-group-guard"),
    ],
)
def test_guard_cutover_never_at_once(field: str, value: object) -> None:
    data = evidence()
    data[field] = value
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invocation_id", "a" * 32),
        ("trigger", "manual"),
        ("started_at", stamp(609)),
        ("last_trigger_at", stamp(309)),
        ("completed_at", stamp(3610)),
        ("completed_at", stamp(610)),
        ("observed_at", stamp(613)),
        ("status", "FAIL"),
        ("broker", "other:9092"),
        ("bus_lane", "other"),
        ("reap_detail", "reap stopped at the first error: X"),
        ("reap_detail", "reaped 2 of 3 eligible Empty groups"),
    ],
)
def test_guard_cutover_parity_runs(field: str, value: object) -> None:
    data = evidence()
    data["runs"][1][field] = value
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1


def test_guard_cutover_parity_requires_positive_reaping_and_two_runs() -> None:
    data = evidence()
    data["runs"][0]["reap_detail"] = "reaped 0 of 0 eligible Empty groups"
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1
    data = evidence()
    data["runs"].pop()
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1
    data = evidence()
    data["runs"][0]["completed_at"] = stamp(611)
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1
    data = evidence()
    data["implementation_digests"]["lab_dispatch"] = "b" * 64
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rollback_uninstall", readback(611)),
        ("rollback_stop", readback(699)),
        ("rollback_stop", readback(701, exit_code=1)),
        ("rollback_reset_failed", readback(702, exit_code=1)),
        ("rollback_uninstall", readback(700, exit_code=1)),
        ("rollback_reset_failed", readback(700)),
        ("rollback_is_active", readback(701, exit_code=3, stdout="inactive\n")),
        ("rollback_is_active", readback(703, exit_code=0, stdout="active\n")),
        ("rollback_is_active", readback(703, exit_code=4, stdout="unknown\n")),
        ("mac_reinstall", readback(702)),
        ("mac_reinstall", readback(704, exit_code=1)),
    ],
)
def test_guard_cutover_rollback_order(field: str, value: object) -> None:
    data = evidence()
    data[field] = value
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1


@pytest.mark.parametrize("field", list(evidence()))
def test_guard_cutover_missing_readbacks(field: str) -> None:
    data = evidence()
    del data[field]
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1


def test_guard_cutover_refuses_naive_time_and_unknown_fields() -> None:
    data = evidence()
    data["mac_unload"]["observed_at"] = "2026-11-03T12:00:00"
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1
    data = evidence()
    data["mac_unload"]["exit_code"] = "0"
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1
    data = evidence()
    data["unrecognized"] = True
    assert verify_cutover(data, host="h053", interval_seconds=300).exit_code == 1
    assert verify_cutover([], host="h053", interval_seconds=300).exit_code == 1


def test_guard_cutover_cli_reads_without_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "absent"))
    path = tmp_path / "cutover.json"
    path.write_text(json.dumps(evidence()))
    argv = ["verify-cutover", "--host", "h053", "--evidence", str(path)]
    assert schedule_cli.main(argv) == 0
    assert "readbacks satisfy" in capsys.readouterr().out
    broken = copy.deepcopy(evidence())
    broken["mac_absent_after_other_install"]["exit_code"] = 0
    path.write_text(json.dumps(broken))
    assert schedule_cli.main(argv) == 1
    assert "REFUSED" in capsys.readouterr().err
    path.write_text("{broken")
    assert schedule_cli.main(argv) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    path.unlink()
    assert schedule_cli.main(argv) == 3
