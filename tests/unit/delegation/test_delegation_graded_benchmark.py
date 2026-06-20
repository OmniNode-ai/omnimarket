# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""P4 graded delegation benchmark proof (OMN-13369)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.run_delegation_graded_benchmark import _build_packet, main


@pytest.mark.unit
def test_delegation_graded_benchmark_passes_all_cells() -> None:
    packet = _build_packet()

    assert packet["passed"], packet["failures"]
    assert packet["ticket"] == "OMN-13369"
    assert packet["summary"]["benchmark_classes"] == ["easy", "hard", "medium"]
    assert packet["summary"]["negative_controls"] >= 2


@pytest.mark.unit
def test_report_rows_include_required_p4_fields() -> None:
    packet = _build_packet()

    for case in packet["cases"]:
        for field in (
            "model",
            "tier",
            "task_class",
            "score",
            "required_bar",
            "escalation_count",
            "terminal_status",
            "classification",
        ):
            assert field in case
        assert "quality_gate_result_topic" in case["attempts"][0]["event_refs"]
        assert case["projection_rows"]["correlation_trace"]["topic"].startswith(
            "onex.snapshot.projection.delegation."
        )


@pytest.mark.unit
def test_negative_controls_complete_without_escalation() -> None:
    packet = _build_packet()
    controls = [case for case in packet["cases"] if case["negative_control"]]

    assert controls
    for case in controls:
        assert case["terminal_status"] == "completed"
        assert case["escalation_count"] == 0
        assert case["actual_score"] >= case["required_bar"]


@pytest.mark.unit
def test_medium_and_hard_cells_prove_gradient() -> None:
    packet = _build_packet()
    by_id = {case["id"]: case for case in packet["cases"]}

    medium = by_id["medium_summary_escalates_once"]
    assert medium["escalation_count"] == 1
    assert medium["terminal_status"] == "completed"
    assert medium["attempts"][0]["actual_score"] < medium["required_bar"]
    assert medium["actual_score"] >= medium["required_bar"]

    hard = by_id["hard_research_reaches_ceiling"]
    assert hard["escalation_count"] == 2
    assert hard["terminal_status"] == "completed"
    assert hard["tier"] == "claude"
    assert hard["attempts"][0]["actual_score"] < hard["required_bar"]
    assert hard["attempts"][1]["actual_score"] < hard["required_bar"]
    assert hard["actual_score"] >= hard["required_bar"]


@pytest.mark.unit
def test_marker_controls_have_expected_terminal_classification() -> None:
    packet = _build_packet()
    by_id = {case["id"]: case for case in packet["cases"]}

    marker_light = by_id["negative_correct_marker_light_passes"]
    assert marker_light["terminal_status"] == "completed"
    assert marker_light["escalation_count"] == 0
    assert marker_light["classification"] == "pass"

    marker_rich_wrong = by_id["marker_rich_wrong_fails"]
    assert marker_rich_wrong["terminal_status"] == "failed"
    assert marker_rich_wrong["classification"] == "fail"


@pytest.mark.unit
def test_cli_emit_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "delegation_graded_benchmark.json"

    assert main(["--emit", str(output)]) == 0
    packet = json.loads(output.read_text())
    assert packet["passed"] is True
    assert packet["summary"]["n_cases"] == 6
