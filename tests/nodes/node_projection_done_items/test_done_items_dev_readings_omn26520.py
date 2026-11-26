# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded dev readings behind OMN-26520 AC5, as measured.

What the readings show, no more: the runtime read healthy, and the done_items
row count rose from 136 (23:04Z and 23:15Z reads) to 139 while done events
arrived. A later pair of reads 627 s apart read 139 both times, so a rise inside
a ten minute window is only shown where a done event arrived in it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

READINGS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "dev_done_items_readings_2026-11-25.json"
    ).read_text()
)


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_runtime_read_healthy() -> None:
    health = READINGS["health"]
    assert health["container"] == "omninode-runtime"
    assert health["docker_health"] == "healthy"
    assert health["failing_streak"] == 0


def test_row_count_rose_while_done_events_arrived() -> None:
    reads = READINGS["done_items_reads"]
    assert [r["rows"] for r in reads] == [136, 136, 139]
    assert _t(reads[-1]["max_projected_at"]) > _t(reads[0]["max_projected_at"])
    # reads are in time order
    stamps = [_t(r["read_at"]) for r in reads]
    assert stamps == sorted(stamps)


def test_flat_ten_minute_window_is_recorded_not_hidden() -> None:
    window = READINGS["ten_minute_window_after_139"]
    assert window["rows_first"] == window["rows_second"] == 139
    assert 600 <= window["seconds_apart"] < 660
