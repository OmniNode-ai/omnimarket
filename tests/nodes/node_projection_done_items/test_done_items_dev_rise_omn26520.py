# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 2026-11-26 dev readings behind OMN-26520 AC5, as measured.

Three reads of the dev runtime. The first (runtime unhealthy, container replaced
two minutes later) and the second (healthy) read 142 rows, ten minutes apart:
flat. The third, 11 min 14 s after the second, reads 143 with the runtime
healthy: the ten minute rise AC5 asks for, between the second and third reads.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

READINGS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "dev_done_items_readings_2026-11-26.json"
    ).read_text()
)
READS = READINGS["reads"]


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_reads_are_in_time_order() -> None:
    stamps = [_t(r["read_at"]) for r in READS]
    assert stamps == sorted(stamps)


def test_row_count_rose_across_two_healthy_reads_ten_minutes_apart() -> None:
    before, after = READS[1], READS[2]
    gap = (_t(after["read_at"]) - _t(before["read_at"])).total_seconds()
    assert gap >= 600
    assert before["docker_health"] == after["docker_health"] == "healthy"
    assert before["failing_streak"] == after["failing_streak"] == 0
    assert after["rows"] > before["rows"]
    assert _t(after["max_projected_at"]) > _t(before["max_projected_at"])


def test_flat_pair_and_unhealthy_read_are_recorded_not_hidden() -> None:
    first, second = READS[0], READS[1]
    assert first["docker_health"] == "unhealthy"
    assert first["rows"] == second["rows"]
    assert (_t(second["read_at"]) - _t(first["read_at"])).total_seconds() >= 600


def test_writer_filter_was_live_in_the_window() -> None:
    filtered = READINGS["filtered_events_logged"]
    assert filtered["count"] > 0
    assert "EVENT_NOT_APPLICABLE" in filtered["line"]
    assert "DoneItemsProjectionWriter" in filtered["line"]
