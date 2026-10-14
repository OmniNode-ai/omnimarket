# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour tests for the difficulty tiers and the unpinned-ladder trace (OMN-20844).

Two claims are made here and each has a negative control:

* A task's difficulty tier is a pure function of what the bundle measures about
  its input (its prompt length), fixed per class, so a tier does not move when
  tasks are added, and the committed test and code_generation tasks fill every
  tier with at least eight tasks.
* A hard-tier task sent through the real, unpinned ladder leaves a trace that
  names the rung that finally answered, how many escalations it took and why
  each lower rung was refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.delegation_ladder.run_ladder import (
    RUNGS,
    ladder_trace,
    load_bundles,
    rung_command,
)
from benchmarks.delegation_ladder.summarise_results import (
    DIFFICULTY_ORDER,
    difficulty_tier,
    first_failing_tier,
    render_difficulty_table,
    render_ladder_table,
    tier_rung_table,
)

pytestmark = pytest.mark.unit

MINIMUM_PER_TIER = 8


def _record(
    task_id: str, task_type: str, model_rung: str, *, accepted: bool
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "task_type": task_type,
        "model_rung": model_rung,
        "accepted": accepted,
        "delivered": True,
        "scorer_outcome": "pass" if accepted else "fail",
        "wall_ms": 10_000,
        "model_latency_ms": 8_000,
        "cost_usd": None,
    }


# --------------------------------------------------------------------------
# The tier is a function of the input, fixed per class
# --------------------------------------------------------------------------


def test_a_test_task_changes_tier_exactly_at_the_class_thresholds() -> None:
    assert difficulty_tier("test", 2_999) == "easy"
    assert difficulty_tier("test", 3_000) == "medium"
    assert difficulty_tier("test", 4_999) == "medium"
    assert difficulty_tier("test", 5_000) == "hard"


def test_a_code_generation_task_changes_tier_exactly_at_the_class_thresholds() -> None:
    assert difficulty_tier("code_generation", 6_999) == "easy"
    assert difficulty_tier("code_generation", 7_000) == "medium"
    assert difficulty_tier("code_generation", 8_999) == "medium"
    assert difficulty_tier("code_generation", 9_000) == "hard"


def test_a_class_without_declared_thresholds_has_no_tier_rather_than_a_guess() -> None:
    assert difficulty_tier("document", 100_000) is None


def test_the_committed_test_and_code_generation_tasks_fill_every_tier() -> None:
    counts: dict[tuple[str, str], int] = {}
    for bundle in load_bundles(None):
        tier = difficulty_tier(bundle.task_type, len(bundle.prompt))
        if tier is not None:
            key = (bundle.task_type, tier)
            counts[key] = counts.get(key, 0) + 1
    for task_type in ("test", "code_generation"):
        for tier in DIFFICULTY_ORDER:
            assert counts.get((task_type, tier), 0) >= MINIMUM_PER_TIER, (
                task_type,
                tier,
                counts,
            )


def test_only_the_two_tiered_classes_receive_a_tier() -> None:
    tiered = {
        bundle.task_type
        for bundle in load_bundles(None)
        if difficulty_tier(bundle.task_type, len(bundle.prompt)) is not None
    }
    assert tiered == {"test", "code_generation"}


# --------------------------------------------------------------------------
# Accepted share per rung per tier
# --------------------------------------------------------------------------


def _tiers() -> dict[str, str]:
    return {
        **{f"T{i}": "easy" for i in range(8)},
        **{f"T{i}": "medium" for i in range(8, 16)},
        **{f"T{i}": "hard" for i in range(16, 24)},
    }


def _rung_records(model_rung: str, passes: tuple[int, int, int]) -> list[dict]:
    rows = []
    for index in range(24):
        band = index // 8
        within = index % 8
        rows.append(
            _record(
                f"T{index}",
                "test",
                model_rung,
                accepted=within < passes[band],
            )
        )
    return rows


def test_one_row_per_class_tier_and_rung_with_share_and_interval() -> None:
    records = _rung_records("rung-a", (8, 5, 1))
    rows = tier_rung_table(records, _tiers())
    assert [(r["task_type"], r["difficulty"]) for r in rows] == [
        ("test", "easy"),
        ("test", "medium"),
        ("test", "hard"),
    ]
    easy, medium, hard = rows
    assert (easy["accepted"], easy["tasks"]) == (8, 8)
    assert (medium["accepted"], medium["tasks"]) == (5, 8)
    assert (hard["accepted"], hard["tasks"]) == (1, 8)
    assert medium["accepted_share"] == pytest.approx(5 / 8)
    # Eight tasks cannot pin a share: the interval is wide, and it is carried.
    assert hard["interval_low"] < 0.125 < hard["interval_high"]
    assert hard["interval_high"] - hard["interval_low"] > 0.3


def test_a_task_with_no_tier_is_left_out_not_filed_under_a_default() -> None:
    records = _rung_records("rung-a", (8, 8, 8))
    records.append(_record("DOC-1", "document", "rung-a", accepted=True))
    rows = tier_rung_table(records, _tiers())
    assert {r["task_type"] for r in rows} == {"test"}
    assert sum(r["tasks"] for r in rows) == 24


def test_a_gate_refused_row_counts_against_the_tier_not_out_of_it() -> None:
    records = _rung_records("rung-a", (8, 8, 8))
    refused = _record("T20", "test", "rung-a", accepted=False)
    refused["delivered"] = False
    refused["scorer_outcome"] = "no_answer"
    records = [r for r in records if r["task_id"] != "T20"] + [refused]
    hard = [r for r in tier_rung_table(records, _tiers()) if r["difficulty"] == "hard"]
    assert (hard[0]["accepted"], hard[0]["tasks"], hard[0]["no_answer"]) == (7, 8, 1)


# --------------------------------------------------------------------------
# The tier where a rung starts failing
# --------------------------------------------------------------------------


def _first_failing(passes: tuple[int, int, int]) -> dict[str, str | None]:
    rows = tier_rung_table(_rung_records("rung-a", passes), _tiers())
    return first_failing_tier(rows)["test", "rung-a"]


def test_a_rung_that_clears_every_tier_never_starts_failing() -> None:
    got = _first_failing((8, 8, 8))
    assert got == {"first_miss": None, "first_majority_fail": None}


def test_a_rung_that_misses_one_task_starts_missing_where_it_first_misses() -> None:
    got = _first_failing((8, 7, 7))
    assert got["first_miss"] == "medium"
    assert got["first_majority_fail"] is None


def test_a_rung_starts_failing_at_the_first_tier_it_loses_the_majority() -> None:
    got = _first_failing((8, 5, 1))
    assert got["first_miss"] == "medium"
    assert got["first_majority_fail"] == "hard"
    assert _first_failing((3, 2, 0))["first_majority_fail"] == "easy"


def test_the_rendered_tier_table_names_each_row_and_where_the_rung_fails() -> None:
    rows = tier_rung_table(_rung_records("rung-a", (8, 5, 1)), _tiers())
    text = render_difficulty_table(rows, first_failing_tier(rows))
    assert "### test" in text
    for tier in DIFFICULTY_ORDER:
        assert f"| rung-a | {tier} |" in text
    assert "5/8" in text
    assert "1/8" in text
    assert "first majority failure: hard" in text


# --------------------------------------------------------------------------
# The unpinned ladder
# --------------------------------------------------------------------------


def test_the_unpinned_ladder_command_carries_no_backend_pin() -> None:
    argv = rung_command(
        RUNGS["unpinned-ladder"],
        prompt_file=Path("/scratch/p.txt"),
        task_type="code_generation",
        ticket="OMN-20844",
        lane="lane-x",
        state_root=Path("/state"),
        onex=Path("/onex"),
        harness_script=Path("/h.py"),
    )
    assert argv[:3] == ["bash", "/onex", "delegate"]
    assert "--backend-id" not in argv
    assert argv[argv.index("--task-type") + 1] == "code_generation"


def test_the_unpinned_ladder_is_never_in_the_default_rung_set() -> None:
    assert RUNGS["unpinned-ladder"].in_default_set is False


def _attempt(backend: str, tier: str, decision: str, reason: str, error: str) -> dict:
    return {
        "backend_id": backend,
        "tier": tier,
        "model_id": f"{backend}-model",
        "acceptance_decision": decision,
        "acceptance_reason": reason,
        "error_message": error,
        "failure_class": None if decision == "accept" else "quality_gate_failed",
    }


def test_a_task_the_second_rung_answered_took_one_escalation_and_names_why() -> None:
    trace = ladder_trace(
        [
            _attempt(
                "local-coder", "local", "climb", "deterministic_floor_failed", "x"
            ),
            _attempt("openrouter-nemotron-ultra", "cheap_frontier", "accept", "ok", ""),
        ]
    )
    assert trace["answered_by"] == "openrouter-nemotron-ultra"
    assert trace["answered_tier"] == "cheap_frontier"
    assert trace["escalations"] == 1
    assert trace["refused"] == [
        {
            "backend_id": "local-coder",
            "tier": "local",
            "decision": "climb",
            "reason": "deterministic_floor_failed",
            "error": "x",
        }
    ]


def test_a_task_answered_by_the_first_rung_took_no_escalation() -> None:
    trace = ladder_trace([_attempt("local-coder", "local", "accept", "ok", "")])
    assert (trace["answered_by"], trace["escalations"], trace["refused"]) == (
        "local-coder",
        0,
        [],
    )


def test_a_ladder_that_never_accepted_names_no_answering_rung() -> None:
    trace = ladder_trace(
        [
            _attempt("local-coder", "local", "climb", "gate", "a"),
            _attempt("cloud-gemini-2-5-flash", "claude", "terminate", "gate", "b"),
        ]
    )
    assert trace["answered_by"] is None
    assert trace["escalations"] == 1
    assert [r["backend_id"] for r in trace["refused"]] == [
        "local-coder",
        "cloud-gemini-2-5-flash",
    ]


def test_a_ladder_with_no_attempts_is_an_empty_trace_not_an_error() -> None:
    assert ladder_trace([]) == {
        "answered_by": None,
        "answered_tier": None,
        "escalations": 0,
        "refused": [],
    }


def _ladder_record(
    task_id: str, trace: dict, *, accepted: bool, wall_ms: int = 60_000
) -> dict:
    return {
        "task_id": task_id,
        "task_type": "test",
        "difficulty": "hard",
        "accepted": accepted,
        "scorer_outcome": "pass" if accepted else "fail",
        "wall_ms": wall_ms,
        "ladder": trace,
    }


def test_the_ladder_table_gives_one_row_per_task_with_the_why_of_each_refusal() -> None:
    climbed = ladder_trace(
        [
            _attempt(
                "local-coder", "local", "climb", "deterministic_floor_failed", "x"
            ),
            _attempt("openrouter-nemotron-ultra", "cheap_frontier", "accept", "ok", ""),
        ]
    )
    stayed = ladder_trace([_attempt("local-coder", "local", "accept", "ok", "")])
    text = render_ladder_table(
        [
            _ladder_record("R4-05", climbed, accepted=True, wall_ms=90_000),
            _ladder_record("R4-07", stayed, accepted=False, wall_ms=30_000),
        ]
    )
    assert "| R4-05 | test | openrouter-nemotron-ultra | 1 | " in text
    assert "local-coder: deterministic_floor_failed" in text
    assert "| R4-07 | test | local-coder | 0 | " in text
    assert "90.0" in text


def test_the_ladder_table_counts_who_answered_and_how_often_it_climbed() -> None:
    climbed = ladder_trace(
        [
            _attempt("local-coder", "local", "climb", "gate", "x"),
            _attempt("openrouter-nemotron-ultra", "cheap_frontier", "accept", "ok", ""),
        ]
    )
    stayed = ladder_trace([_attempt("local-coder", "local", "accept", "ok", "")])
    nobody = ladder_trace([_attempt("local-coder", "local", "terminate", "gate", "x")])
    text = render_ladder_table(
        [
            _ladder_record("A", climbed, accepted=True),
            _ladder_record("B", stayed, accepted=True),
            _ladder_record("C", stayed, accepted=False),
            _ladder_record("D", nobody, accepted=False),
        ]
    )
    assert "- local-coder answered 2 of 4" in text
    assert "- openrouter-nemotron-ultra answered 1 of 4" in text
    assert "- no rung answered 1 of 4" in text
    assert "climbed at least once: 1 of 4" in text


def test_a_ladder_run_that_returned_no_receipt_is_a_row_answered_by_nobody() -> None:
    record = _ladder_record("R4-05", {}, accepted=False)
    del record["ladder"]
    text = render_ladder_table([record])
    assert "| R4-05 | test | none | 0 | no | 60.0 | - |" in text
    assert "- no rung answered 1 of 1" in text


def test_the_ladder_table_counts_answers_the_gate_took_and_the_scorer_failed() -> None:
    stayed = ladder_trace([_attempt("local-coder", "local", "accept", "ok", "")])
    nobody = ladder_trace([_attempt("local-coder", "local", "terminate", "gate", "x")])
    text = render_ladder_table(
        [
            _ladder_record("A", stayed, accepted=True),
            _ladder_record("B", stayed, accepted=False),
            _ladder_record("C", stayed, accepted=False),
            _ladder_record("D", nobody, accepted=False),
        ]
    )
    # B and C passed the gate and were wrong; D never passed it: only B and C count.
    assert "gate accepted, scorer failed: 2 of 4" in text
