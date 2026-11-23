# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_host_lane_path_share_compute, through its entry point and its JSON entry (OMN-25931)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_host_lane_path_share_compute import __main__ as entry
from omnimarket.nodes.node_host_lane_path_share_compute.models.model_lane_path_share import (
    ModelHostLanePaths,
    ModelLanePathReading,
    ModelLanePathShareRequest,
)
from omnimarket.nodes.node_host_lane_path_share_compute.node import (
    NodeHostLanePathShareCompute,
)

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)


def _request(on_drive: int, on_root: int) -> ModelLanePathShareRequest:
    return ModelLanePathShareRequest(
        now=NOW,
        hosts=(
            ModelHostLanePaths(
                host_name="h053",
                owner="orchestrator-31d9-swarm",
                data_drive_mounts=("/mnt/h053-nvme",),
                paths=(
                    ModelLanePathReading(
                        kind="run-dir",
                        path="/data/lab-run/runs",
                        mount_point="/mnt/h053-nvme",
                        bytes_used=on_drive,
                    ),
                    ModelLanePathReading(
                        kind="worktrees",
                        path="/srv/lab/worktrees",
                        mount_point="/",
                        bytes_used=on_root,
                    ),
                ),
            ),
        ),
    )


def test_lane_path_share_golden_chain_alarm_row_through_the_node() -> None:
    result = NodeHostLanePathShareCompute().handle(_request(9, 91))
    assert [(h.host_name, h.state) for h in result.hosts] == [("h053", "alarm")]
    assert "owner=orchestrator-31d9-swarm" in result.rows[0]


def test_lane_path_share_golden_chain_json_entry_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(_request(11, 89).model_dump_json()))
    assert entry.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["hosts"][0]["state"] == "ok"
    monkeypatch.setattr("sys.stdin", io.StringIO(_request(0, 0).model_dump_json()))
    assert entry.main() == 65
