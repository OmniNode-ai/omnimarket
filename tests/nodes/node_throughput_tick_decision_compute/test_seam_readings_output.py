# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the declared seam_readings output through the tick handler."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)


@pytest.mark.unit
@pytest.mark.parametrize("with_reading", [False, True])
def test_seam_readings_output_records_missing_or_consumed_reading(
    with_reading: bool,
) -> None:
    window = {"start": "2026-11-02T12:00:00Z", "end": "2026-11-02T12:10:00Z"}
    reading = {
        "seam_id": "VS-02",
        "window": window,
        "crossings": 1,
        "mismatched": 1,
        "unreadable": 0,
        "read_at": "2026-11-02T12:11:00Z",
    }
    request = ModelThroughputTickRequest.model_validate(
        {
            "now": "2026-11-02T12:30:00Z",
            "launchctl": "not_loaded",
            "seam_readings": {
                "window": window,
                "finalized": True,
                "publish_steps_ok": True,
                "expected_seam_ids": ["VS-02"],
                "readings": [reading] if with_reading else [],
            },
        }
    )
    result = HandlerThroughputTickDecision().handle(request)
    assert result.seam_readings is not None
    assert result.seam_readings.expected_ids == ("VS-02",)
    if with_reading:
        assert result.seam_readings.consumed_ids == ("VS-02",)
        assert result.seam_readings.missing_ids == ("VS-02",)
        assert result.seam_readings.unknown_ids == ()
        assert "seam:VS-02" in result.missing
    else:
        assert result.seam_readings.consumed_ids == ()
        assert result.seam_readings.missing_ids == ()
        assert result.seam_readings.unknown_ids == ("VS-02",)
        assert "seam:VS-02" in result.unknown
