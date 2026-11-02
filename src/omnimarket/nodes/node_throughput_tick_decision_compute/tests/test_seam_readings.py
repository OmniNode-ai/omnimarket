# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6: the throughput tick consumes the seam readings of a finalized window (VS-17).

Tonight's board drew one of three declared lines and every publish step read ok. A finalized
window whose publish steps are all ok and whose VS-02 reading is mismatched must raise
a MISSING finding for the loop `seam:VS-02`; a reading that is absent must raise `UNKNOWN`.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
    ModelThroughputTickResult,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-11-02T12:30:00Z"
WINDOW = {"start": "2026-11-02T12:00:00Z", "end": "2026-11-02T12:10:00Z"}
VS02_LOOP = "seam:VS-02"
EXPECTED = [f"VS-{n:02d}" for n in range(1, 17)]


def reading(
    seam_id: str, *, mismatched: int = 0, unreadable: int = 0
) -> dict[str, Any]:
    return {
        "seam_id": seam_id,
        "window": WINDOW,
        "crossings": 0 if unreadable else 1,
        "mismatched": mismatched,
        "unreadable": unreadable,
        "examples": ["line-b"] if mismatched else [],
        "read_at": "2026-11-02T12:11:00Z",
    }


def facts(readings: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "window": WINDOW,
        "finalized": True,
        "publish_steps_ok": True,
        "expected_seam_ids": EXPECTED,
        "readings": readings,
        "declared_seam_ids": ["VS-03", "VS-05"],
        **extra,
    }


def run(**overrides: Any) -> ModelThroughputTickResult:
    payload: dict[str, Any] = {"now": NOW, "launchctl": "not_loaded", **overrides}
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def all_matched() -> list[dict[str, Any]]:
    return [reading(seam) for seam in EXPECTED]


def seam_missing(result: ModelThroughputTickResult) -> list[str]:
    return [m for m in result.missing if m.startswith("seam:")]


def seam_unknown(result: ModelThroughputTickResult) -> list[str]:
    return [m for m in result.unknown if m.startswith("seam:")]


def seam_lines(result: ModelThroughputTickResult) -> list[str]:
    return [line for line in result.lines if "seam:" in line]


def test_seam_readings_mismatched_vs02_with_every_publish_step_ok_is_missing() -> None:
    readings = [reading(s, mismatched=2 if s == "VS-02" else 0) for s in EXPECTED]
    result = run(seam_readings=facts(readings))
    [line] = [line for line in seam_lines(result) if line.startswith("MISSING")]
    assert line.startswith(f"MISSING {VS02_LOOP} | "), line
    assert "publish steps ok" in line
    assert " | FIX: " in line
    assert seam_missing(result) == ["seam:VS-02"]
    assert seam_unknown(result) == []
    assert result.exit_code == 1
    assert "seam-readings" in result.checked


def test_seam_readings_absent_reading_is_unknown() -> None:
    readings = [r for r in all_matched() if r["seam_id"] != "VS-05"]
    result = run(seam_readings=facts(readings))
    [line] = [line for line in seam_lines(result) if line.startswith("UNKNOWN")]
    assert line.startswith("UNKNOWN seam:VS-05 | "), line
    assert seam_unknown(result) == ["seam:VS-05"]
    assert seam_missing(result) == []
    assert result.exit_code == 1


def test_seam_readings_unreadable_reading_is_unknown() -> None:
    readings = [reading(s, unreadable=1 if s == "VS-09" else 0) for s in EXPECTED]
    result = run(seam_readings=facts(readings))
    assert seam_unknown(result) == ["seam:VS-09"]
    assert seam_missing(result) == []


def test_seam_readings_all_matched_raises_nothing_and_notes_the_declared_seams() -> (
    None
):
    result = run(seam_readings=facts(all_matched()))
    assert seam_missing(result) == []
    assert seam_unknown(result) == []
    [line] = seam_lines(result)
    assert line.startswith("NOTE seam:declared "), line
    assert "VS-03" in line
    assert "VS-05" in line


def test_seam_readings_a_window_not_finalized_is_judged_by_nothing() -> None:
    readings = [reading(s, mismatched=1) for s in EXPECTED]
    result = run(seam_readings=facts(readings, finalized=False))
    assert seam_missing(result) == []
    assert seam_unknown(result) == []
    assert any(line.startswith("NOTE seam:window") for line in result.lines)


def test_seam_readings_without_the_field_the_tick_reads_as_before() -> None:
    result = run()
    assert seam_lines(result) == []
    assert "seam-readings" not in result.checked
    assert result.seam_readings is None


def test_seam_readings_verdict_lists_expected_consumed_and_findings() -> None:
    readings = [r for r in all_matched() if r["seam_id"] != "VS-16"]
    readings[1] = reading("VS-02", mismatched=1)
    result = run(seam_readings=facts(readings))
    verdict = result.seam_readings
    assert verdict is not None
    assert verdict.expected_ids == tuple(EXPECTED)
    assert verdict.consumed_ids == tuple(r["seam_id"] for r in readings)
    assert verdict.missing_ids == ("VS-02",)
    assert verdict.unknown_ids == ("VS-16",)


def test_seam_readings_a_second_reading_of_one_seam_takes_the_later_read_at() -> None:
    older = reading("VS-02", mismatched=1)
    newer = {**reading("VS-02"), "read_at": "2026-11-02T12:12:00Z"}
    readings = [r for r in all_matched() if r["seam_id"] != "VS-02"] + [older, newer]
    result = run(seam_readings=facts(readings))
    assert seam_missing(result) == []
