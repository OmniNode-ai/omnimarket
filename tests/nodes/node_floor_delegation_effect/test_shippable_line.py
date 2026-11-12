# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shippable line: delegation is shippable only when every scenario passes on its latest scheduled run (OMN-23081)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_tracker import (
    build_tracker,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationTracker,
    ModelScenarioRead,
)

from .support import NOW, RT, all_reads, inputs, read, run


def _build(reads: tuple[ModelScenarioRead, ...]) -> ModelDelegationTracker:
    return build_tracker(RT, inputs(reads=reads), NOW)


def test_every_scenario_passing_is_shippable() -> None:
    doc = _build(all_reads("success"))
    assert doc.shippable is True
    assert doc.blocking == ()
    assert doc.counts.scenarios_passing == doc.counts.scenarios_total == 9
    assert doc.shippable_line.startswith("Delegation is shippable")


def test_control_one_failing_scenario_makes_it_not_shippable() -> None:
    """The failing control: eight passes and one failure must not read as shippable."""
    reads = list(all_reads("success"))
    reads[6] = read("R7", run("failure", 10))
    doc = _build(tuple(reads))
    assert doc.shippable is False
    assert doc.blocking == ("R7",)
    assert doc.counts.scenarios_passing == 8
    assert doc.counts.scenarios_failing == 1
    assert "8 of 9" in doc.shippable_line
    failing = next(s for s in doc.scenarios if s.id == "R7")
    assert failing.state == "fail"
    assert failing.result_text == "Failed (failure)"


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "timed_out", "skipped", ""]
)
def test_anything_but_success_is_not_a_pass(conclusion: str) -> None:
    reads = list(all_reads("success"))
    reads[0] = read("R1", run(conclusion, 10))
    assert _build(tuple(reads)).shippable is False


def test_the_latest_finished_run_decides_not_an_older_green_one() -> None:
    reads = list(all_reads("success"))
    reads[0] = read("R1", run("failure", 5), run("success", 65), run("success", 125))
    doc = _build(tuple(reads))
    r1 = next(s for s in doc.scenarios if s.id == "R1")
    assert r1.state == "fail"
    assert r1.recent == ("fail", "pass", "pass")
    assert doc.shippable is False


def test_a_run_still_going_does_not_hide_the_latest_finished_result() -> None:
    reads = list(all_reads("success"))
    reads[0] = read("R1", run("", 1, status="in_progress"), run("success", 61))
    doc = _build(tuple(reads))
    r1 = next(s for s in doc.scenarios if s.id == "R1")
    assert r1.state == "pass"
    assert r1.in_progress is True


def test_a_workflow_that_does_not_exist_is_not_built_and_names_its_builder() -> None:
    reads = list(all_reads("success"))
    reads[1] = read("R2", kind="no_workflow", reason="HTTP 404")
    doc = _build(tuple(reads))
    r2 = next(s for s in doc.scenarios if s.id == "R2")
    assert r2.state == "no_workflow"
    assert r2.result_text == "Not built yet"
    assert "OMN-23172" in r2.reason
    assert doc.counts.scenarios_not_built == 1
    assert doc.shippable is False


def test_no_finished_scheduled_run_is_not_a_pass() -> None:
    reads = list(all_reads("success"))
    reads[2] = read("R3")
    doc = _build(tuple(reads))
    assert next(s for s in doc.scenarios if s.id == "R3").state == "no_run"
    assert doc.shippable is False


def test_a_scenario_that_could_not_be_read_is_unread_never_a_pass() -> None:
    reads = list(all_reads("success"))
    reads[3] = read("R4", kind="unread", reason="exit 124: timed out after 25 s")
    doc = _build(tuple(reads))
    r4 = next(s for s in doc.scenarios if s.id == "R4")
    assert r4.state == "unread"
    assert "timed out" in r4.reason
    assert doc.shippable is False


def test_a_read_older_than_its_limit_is_not_a_pass_even_when_it_was_green() -> None:
    reads = list(all_reads("success"))
    reads[4] = read(
        "R5",
        run("success", 10),
        read_minutes_ago=RT.scenario_reads_max_age_seconds // 60 + 5,
    )
    doc = _build(tuple(reads))
    assert next(s for s in doc.scenarios if s.id == "R5").state == "stale"
    assert doc.shippable is False


def test_no_scenario_file_makes_every_scenario_unread() -> None:
    doc = build_tracker(RT, inputs(reads=None), NOW)
    assert doc.shippable is False
    assert doc.counts.scenarios_unread == 9


def test_a_scenario_with_no_read_in_the_file_is_unread() -> None:
    reads = all_reads("success")[:-1]
    doc = _build(reads)
    assert next(s for s in doc.scenarios if s.id == "R9").state == "unread"
    assert doc.shippable is False


def test_a_quiet_producer_is_marked_overdue_but_keeps_its_result() -> None:
    reads = list(all_reads("success"))
    reads[0] = read("R1", run("success", 60 * 5))
    doc = _build(tuple(reads))
    r1 = next(s for s in doc.scenarios if s.id == "R1")
    assert r1.state == "pass"
    assert r1.overdue is True
