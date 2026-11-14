# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane count read from the remote-lane runner's receipts (OMN-24489).

A receipt counts when it names the host, is not final, is preparing or running, and its pid, when it has one, is
alive. A receipts directory that cannot be read is an unreadable count, never a measured zero.
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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _dead_pid() -> int:
    """A pid of a process that has exited, or one beyond the table when the pid was reused."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid if not _alive(child.pid) else 2**22 + 12345


def _receipt(directory: Path, lane: str, data: dict[str, Any]) -> None:
    lane_dir = directory / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    (lane_dir / "receipt.json").write_text(json.dumps(data), encoding="utf-8")


def _receipts(directory: Path, *, dead_pid: int) -> None:
    _receipt(directory, "lane-1", {"host": "a", "status": "running"})
    _receipt(
        directory, "lane-2", {"host": "a", "status": "preparing", "pid": os.getpid()}
    )
    _receipt(directory, "lane-3", {"host": "a", "status": "running", "final": True})
    _receipt(directory, "lane-4", {"host": "a", "status": "running", "pid": dead_pid})
    _receipt(directory, "lane-5", {"host": "b", "status": "running"})
    unparsed = directory / "lane-6"
    unparsed.mkdir()
    (unparsed / "receipt.json").write_text("{not json", encoding="utf-8")


def _expected_until(overlay: ModelHostDrainOverlay) -> str:
    until = NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)
    return until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_without_the_final_filter(directory: Path, host_name: str) -> int:
    """The failing control: the same count with the final-receipt filter dropped."""
    count = 0
    for receipt in directory.glob("*/*.json"):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            if (
                data.get("host") == host_name
                and data.get("status") in ("preparing", "running")
                and (not data.get("pid") or _alive(int(data["pid"])))
            ):
                count += 1
        except (OSError, ValueError, TypeError):
            continue
    return count


def test_receipts_count_the_host_s_running_lanes(tmp_path: Path) -> None:
    _receipts(tmp_path, dead_pid=_dead_pid())
    lanes = ReceiptLaneSource(tmp_path)
    # The running and the preparing receipt count; the final one, the dead pid and the unparsable file do not.
    assert lanes.running("a") == 2
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
        f"{_expected_until(overlay)}: lane count unreadable: NOT SAFE to power down"
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
    assert golden == 2
    unfiltered = _count_without_the_final_filter(tmp_path, "a")
    assert unfiltered == 3
    assert unfiltered != golden
