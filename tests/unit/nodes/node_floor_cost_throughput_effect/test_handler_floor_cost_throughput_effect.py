# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cost-and-throughput effect reads the floor's documents from the registry home, keeps dated readings and writes the panel."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_floor_cost_throughput_effect.handlers.handler_floor_cost_throughput_effect import (
    HandlerFloorCostThroughputEffect,
    load_runtime,
)
from omnimarket.nodes.node_floor_cost_throughput_effect.models.model_floor_cost_throughput_effect import (
    ModelFloorCostThroughputRequest,
)

DATA = ".claude_scratch/factory-floor"


def _floor_simple(as_of: str, seen: int, merged: int) -> dict[str, Any]:
    return {
        "generated_at": "2026-11-20T14:58:00Z",
        "numbers": [
            {
                "key": "merges",
                "value": "7",
                "read_at": "2026-11-20T14:56:30Z",
                "read": True,
            }
        ],
        "integration": {
            "status": "read",
            "as_of": as_of,
            "counts": {"merged": merged, "seen_running": seen},
        },
    }


def _run(home: Path, at: str) -> dict[str, Any]:
    clock = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    result = HandlerFloorCostThroughputEffect(clock=lambda: clock).handle(
        ModelFloorCostThroughputRequest(home=str(home))
    )
    assert result.ok, result.detail
    return json.loads(
        (home / DATA / "cost-throughput.json").read_text(encoding="utf-8")
    )


def test_contract_runtime_block_loads() -> None:
    assert load_runtime().output_name == "cost-throughput.json"


def test_with_no_documents_every_figure_reads_unreadable_and_a_file_is_still_written(
    tmp_path: Path,
) -> None:
    panel = _run(tmp_path, "2026-11-20T15:00:00Z")
    assert panel["merges_per_hour"]["display"] == "UNREADABLE"
    assert panel["seen_running_per_hour"]["display"] == "UNREADABLE"
    assert panel["trend"]["status"] == "UNREADABLE"
    assert panel["unreadable_count"] > 0


def test_merges_read_from_the_floor_document_and_the_rate_needs_two_kept_readings(
    tmp_path: Path,
) -> None:
    data = tmp_path / DATA
    data.mkdir(parents=True)
    (data / "floor-simple.json").write_text(
        json.dumps(_floor_simple("2026-11-19T15:00:00Z", 500, 1000)), encoding="utf-8"
    )
    first = _run(tmp_path, "2026-11-19T15:05:00Z")
    assert first["merges_per_hour"]["value"] == 7
    assert first["seen_running_per_hour"]["display"] == "UNREADABLE"
    (data / "floor-simple.json").write_text(
        json.dumps(_floor_simple("2026-11-20T14:59:00Z", 548, 1100)), encoding="utf-8"
    )
    second = _run(tmp_path, "2026-11-20T15:00:00Z")
    kept = json.loads(
        (data / "cost-throughput-readings.json").read_text(encoding="utf-8")
    )["points"]
    assert [p["seen_running"] for p in kept] == [500, 548]
    # 48 changes over the 23 h 59 min between the two readings
    assert second["seen_running_per_hour"]["value"] == pytest.approx(
        48 / (1439 / 60), rel=1e-6
    )
    assert (
        second["seen_running_per_hour"]["source"]["node"] == "node_floor_simple_effect"
    )


def test_a_reading_with_the_same_as_of_is_kept_once(tmp_path: Path) -> None:
    data = tmp_path / DATA
    data.mkdir(parents=True)
    (data / "floor-simple.json").write_text(
        json.dumps(_floor_simple("2026-11-20T14:00:00Z", 548, 1100)), encoding="utf-8"
    )
    _run(tmp_path, "2026-11-20T14:05:00Z")
    _run(tmp_path, "2026-11-20T14:10:00Z")
    kept = json.loads(
        (data / "cost-throughput-readings.json").read_text(encoding="utf-8")
    )["points"]
    assert len(kept) == 1


def test_a_document_that_is_not_json_is_unread_not_a_crash(tmp_path: Path) -> None:
    data = tmp_path / DATA
    data.mkdir(parents=True)
    (data / "floor-simple.json").write_text("{not json", encoding="utf-8")
    panel = _run(tmp_path, "2026-11-20T15:00:00Z")
    assert panel["merges_per_hour"]["display"] == "UNREADABLE"
