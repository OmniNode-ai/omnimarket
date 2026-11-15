# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane count read from the remote-lane runner's receipts (OMN-24489).

A file is a lane receipt only when it parses to a dict carrying the key ``run_id``; a dict without ``run_id`` is
ignored. A receipt that cannot be read raises OSError and one that does not parse as JSON raises ValueError, each
naming the file path. A lane receipt counts when it is not final and it names the host, read from ``host`` or,
failing that, from ``prerequisites.host``; a non-final receipt that names no host raises ValueError. Status and the
launcher ``pid`` no longer matter: a non-final receipt counts whatever its status and even when its launcher pid is
dead. A receipts directory that cannot be read or listed is an unreadable count, never a measured zero.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
    ReceiptLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    ModelHostDrainRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    EXAMPLE_OVERLAY,
    ModelHostDrainOverlay,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def _dead_pid() -> int:
    """A pid of a process that has exited, or one beyond the table when the pid was reused."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _receipt(directory: Path, lane: str, data: dict[str, Any]) -> None:
    lane_dir = directory / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    (lane_dir / "receipt.json").write_text(json.dumps(data), encoding="utf-8")


def _receipts(directory: Path, *, dead_pid: int) -> None:
    _receipt(directory, "lane-1", {"run_id": "r1", "host": "a", "status": "running"})
    _receipt(
        directory,
        "lane-2",
        {"run_id": "r2", "host": "a", "status": "preparing", "pid": os.getpid()},
    )
    _receipt(
        directory,
        "lane-3",
        {"run_id": "r3", "host": "a", "status": "running", "final": True},
    )
    _receipt(
        directory,
        "lane-4",
        {"run_id": "r4", "host": "a", "status": "running", "pid": dead_pid},
    )
    _receipt(directory, "lane-5", {"run_id": "r5", "host": "b", "status": "running"})


def _expected_until(overlay: ModelHostDrainOverlay) -> str:
    until = NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)
    return until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_of(data: dict[str, Any]) -> str | None:
    """The host a receipt names: host when a non-empty string, else prerequisites.host when it is."""
    host = data.get("host")
    if isinstance(host, str) and host:
        return host
    prereq = data.get("prerequisites")
    if isinstance(prereq, dict):
        nested = prereq.get("host")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _count_without_the_final_filter(directory: Path, host_name: str) -> int:
    """The failing control: the same count with the final-receipt filter dropped."""
    count = 0
    for receipt in directory.glob("*/*.json"):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict) or "run_id" not in data:
            continue
        if _host_of(data) == host_name:
            count += 1
    return count


def test_receipts_count_the_host_s_running_lanes(tmp_path: Path) -> None:
    _receipts(tmp_path, dead_pid=_dead_pid())
    lanes = ReceiptLaneSource(tmp_path)
    # lane-1 (running) and lane-2 (preparing) count, and lane-4 (a dead launcher pid) still counts: three lanes.
    # The final receipt does not, and the other host's receipt belongs to b.
    assert lanes.running("a") == 3
    assert lanes.running("b") == 1
    assert lanes.running("c") == 0


def test_an_empty_receipts_directory_counts_zero(tmp_path: Path) -> None:
    empty = tmp_path / "receipts"
    empty.mkdir()
    assert ReceiptLaneSource(empty).running("a") == 0


def test_a_missing_receipts_directory_raises_and_the_line_is_not_safe(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    lanes = ReceiptLaneSource(tmp_path / "absent")
    with pytest.raises(OSError, match="absent"):
        lanes.running("a")
    handler = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=lanes,
        sink=sink,
        clock=lambda: NOW,
        backend="in_memory",
    )
    result = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    assert result.outcome is EnumDrainOutcome.DRAINING
    assert result.lanes_running is None
    assert result.status_line == (
        "example-host-a draining until "
        f"{_expected_until(overlay)}: lane count unreadable; host read none declared: NOT SAFE to power down"
    )


def test_lanes_needs_exactly_one_of_the_two_sources() -> None:
    both = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    both["lanes"]["receipts_dir"] = "/example/receipts"
    with pytest.raises(ValueError, match="exactly one"):
        ModelHostDrainOverlay.model_validate(both)
    neither = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    del neither["lanes"]["count_command"]
    with pytest.raises(ValueError, match="exactly one"):
        ModelHostDrainOverlay.model_validate(neither)


def test_lanes_loads_with_receipts_dir_alone() -> None:
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    del document["lanes"]["count_command"]
    document["lanes"]["receipts_dir"] = "/example/receipts"
    loaded = ModelHostDrainOverlay.model_validate(document)
    assert loaded.lanes.count_command is None
    assert loaded.lanes.receipts_dir == "/example/receipts"


def test_dropping_the_final_filter_reads_a_different_count(tmp_path: Path) -> None:
    """A receipt source that stops filtering final receipts fails the golden assertion."""
    _receipts(tmp_path, dead_pid=_dead_pid())
    golden = ReceiptLaneSource(tmp_path).running("a")
    assert golden == 3
    unfiltered = _count_without_the_final_filter(tmp_path, "a")
    assert unfiltered == 4
    assert unfiltered != golden


def test_a_dead_launcher_pid_receipt_still_counts(tmp_path: Path) -> None:
    _receipt(
        tmp_path,
        "lane-1",
        {"run_id": "r1", "host": "a", "status": "running", "pid": _dead_pid()},
    )
    assert ReceiptLaneSource(tmp_path).running("a") == 1


@pytest.mark.parametrize(
    "status",
    ["placing", "queued", "submitted", "preparing", "running"],
)
def test_a_nonfinal_receipt_counts_whatever_its_status(
    tmp_path: Path, status: str
) -> None:
    _receipt(tmp_path, "lane-1", {"run_id": "r1", "host": "a", "status": status})
    assert ReceiptLaneSource(tmp_path).running("a") == 1


def test_a_nonfinal_receipt_with_no_status_counts(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-1", {"run_id": "r1", "host": "a"})
    assert ReceiptLaneSource(tmp_path).running("a") == 1


def test_an_unreadable_receipt_raises(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lane-1"
    lane_dir.mkdir()
    (lane_dir / "receipt.json").mkdir()
    with pytest.raises(OSError, match=r"receipt\.json"):
        ReceiptLaneSource(tmp_path).running("a")


def test_an_unparseable_receipt_raises(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lane-1"
    lane_dir.mkdir()
    (lane_dir / "receipt.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"receipt\.json"):
        ReceiptLaneSource(tmp_path).running("a")


def test_a_nonfinal_receipt_that_names_no_host_raises(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-1", {"run_id": "r1", "status": "running"})
    with pytest.raises(ValueError, match="names no host"):
        ReceiptLaneSource(tmp_path).running("a")


def test_a_file_without_run_id_is_ignored(tmp_path: Path) -> None:
    # A brief-check file: sha256 and checked_at only, no run_id, not a lane receipt.
    _receipt(
        tmp_path, "lane-1", {"sha256": "deadbeef", "checked_at": "2026-01-01T00:00:00Z"}
    )
    # A file that would count under the old rules but is ignored for lack of a run_id.
    _receipt(tmp_path, "lane-2", {"host": "a", "status": "running"})
    assert ReceiptLaneSource(tmp_path).running("a") == 0


def test_a_final_receipt_does_not_count(tmp_path: Path) -> None:
    _receipt(
        tmp_path,
        "lane-1",
        {"run_id": "r1", "host": "a", "status": "running", "final": True},
    )
    assert ReceiptLaneSource(tmp_path).running("a") == 0


def test_the_host_is_read_from_prerequisites(tmp_path: Path) -> None:
    _receipt(
        tmp_path,
        "lane-1",
        {"run_id": "r1", "status": "running", "prerequisites": {"host": "a"}},
    )
    assert ReceiptLaneSource(tmp_path).running("a") == 1
    assert ReceiptLaneSource(tmp_path).running("b") == 0


def test_the_top_level_host_wins_over_prerequisites(tmp_path: Path) -> None:
    _receipt(
        tmp_path,
        "lane-1",
        {
            "run_id": "r1",
            "status": "running",
            "host": "a",
            "prerequisites": {"host": "b"},
        },
    )
    assert ReceiptLaneSource(tmp_path).running("a") == 1
    assert ReceiptLaneSource(tmp_path).running("b") == 0


def _job(directory: Path, lane: str, run_id: str) -> None:
    """A submitted-job record beside the runner's receipts: it names no host."""
    lane_dir = directory / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    (lane_dir / f"{run_id}.job.json").write_text(
        json.dumps({"run_id": run_id, "lane": lane, "runner": {}, "spec": {}}),
        encoding="utf-8",
    )


def test_a_submitted_job_with_a_final_receipt_beside_it_is_ignored(
    tmp_path: Path,
) -> None:
    _job(tmp_path, "lane-1", "r1")
    (tmp_path / "lane-1" / "r1.json").write_text(
        json.dumps({"run_id": "r1", "host": "a", "final": True, "status": "done"}),
        encoding="utf-8",
    )
    assert ReceiptLaneSource(tmp_path).running("a") == 0


def test_a_submitted_job_with_no_final_receipt_reads_unreadable(
    tmp_path: Path,
) -> None:
    """A lane submitted and not yet placed may run on any host: the count must not read zero."""
    _job(tmp_path, "lane-1", "r1")
    with pytest.raises(ValueError, match="no final receipt"):
        ReceiptLaneSource(tmp_path).running("a")


def test_a_submitted_job_whose_receipt_is_not_final_reads_unreadable(
    tmp_path: Path,
) -> None:
    _job(tmp_path, "lane-1", "r1")
    (tmp_path / "lane-1" / "r1.json").write_text(
        json.dumps({"run_id": "r1", "host": "b", "status": "running"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no final receipt"):
        ReceiptLaneSource(tmp_path).running("a")
